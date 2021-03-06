import enum
import functools
import itertools
import operator
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import numpy as np
from dask import dataframe as dd
from rich.color import Color, parse_rgb_hex
from rich.console import Console, ConsoleOptions, ConsoleRenderable, RenderResult
from rich.layout import Layout
from rich.padding import Padding
from rich.panel import Panel
from rich.pretty import Pretty
from rich.style import Style
from rich.styled import Styled
from rich.table import Column, Table
from rich.text import Text

from dbv.df import Schema

bg_color = Color.from_triplet(parse_rgb_hex("1D1F21"))
bg_color_secondary = Color.from_triplet(parse_rgb_hex("101214"))
fg_color = Color.from_triplet(parse_rgb_hex("C5C8C6"))
yellow = Color.from_triplet(parse_rgb_hex("F0C674"))

header_style = Style(color=yellow, bgcolor=bg_color, bold=True)
header = Text("Database Viewer", justify="center", style=header_style)

body_style = Style(color=fg_color, bgcolor=bg_color)
body_style_secondary = Style(color=fg_color, bgcolor=bg_color_secondary)
body = Panel("Hello Pangolins!", style=body_style)


ESCAPE_KEY = "\x1b"
CTRL_H = "\x08"
BACKSPACE = "\x7f"
CTRL_K = "\x0b"


class Mode(enum.Enum):
    """Mode enum for the interface.

    Modes specified here are shown as options in the modeline; add more modes by
        1. add an element here
        2. add a keyboard shortcut to switch to the mode in keyboard_handler
        3. update __rich__ to change rendering based on the mode.
        4. Add a row to Help.__rich__ for the help string
    """

    SUMMARY = "(s)ummary"
    TABLE = "(t)able"
    HELP = "(?)help"


def mode_line(current_mode: Mode) -> Layout:
    """Render the UI mode line."""
    line = Layout(name="mode_line", size=1)

    inactive_style = "black on white"
    active_style = "black on yellow"

    line.split_row(
        *(
            Text(
                mode.value,
                justify="center",
                style=active_style if mode == current_mode else inactive_style,
            )
            for mode in Mode
        )
    )

    return line


class Help:
    """Rich-renderable command help page."""

    def __init__(self, command_dict: dict):
        self.tables = []
        for title, commands in command_dict.items():
            table = Table(
                title=title,
                expand=True,
                row_styles=[body_style, body_style_secondary],
            )
            table.add_column("Command")
            table.add_column("Short")
            table.add_column("Description")
            for key, command in commands.items():
                table.add_row(key, command.short_description, command.help)
            self.tables.append(table)

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        for table in self.tables:
            yield table


class Summary:
    """Show a summary of the database"""

    # Rich-renderable summary pane for a DataFrame.

    def __init__(self, df: dd.DataFrame):
        self.df = df

    def __rich__(self) -> ConsoleRenderable:
        return Schema.from_df(self.df)


def compile_filter(filter_string: str) -> Callable[[dd.DataFrame], any]:
    """Compile a filter string into a dataframe filter."""

    def _compiled_filter(df: dd.DataFrame) -> any:
        """Compiled filter function."""
        # If it's a column, just return the series.
        # Adding whitespace to a column name compiles as a filter that fails later,
        # so strip whitespace to degrade to at least just showing that column
        if filter_string.strip() in df.columns:
            return getattr(df, filter_string.strip())

        # Add columns into locals so that they may be referred to directly
        locals().update({col: getattr(df, col) for col in df.columns})
        evaluated = eval(filter_string, None, locals())  # noqa: S307

        # Index the df by the evaluated filter
        return df[evaluated]

    return _compiled_filter


class TableView:
    """Show the database as a table."""

    # Rich-renderable summary pane for a DataFrame.

    def __init__(self, df: dd.DataFrame):
        self.df = df
        self._last_page_size = 0
        self._startat = 0
        self._column_startat = 0
        self._filter = CaptureKeyboardInput(
            prompt="filter: ", update=CaptureKeyboardInput.exit_on_return
        )

    @property
    def startat(self) -> int:
        """Which row to start rendering at."""
        return self._startat

    @startat.setter
    def startat(self, startat: int) -> None:
        """Setter for startat."""
        self._startat = min(len(self.df) - 1, max(0, startat))

    @property
    def column_startat(self) -> int:
        """Which column to start rendering at."""
        return self._column_startat

    @column_startat.setter
    def column_startat(self, column_startat: int) -> None:
        """Setter for column_startat."""
        self._column_startat = min(len(self.df.columns) - 1, max(0, column_startat))

    def increment_page(self) -> None:
        """Increment startat by the last known page size."""
        self.startat += self._last_page_size

    def decrement_page(self) -> None:
        """Decrement startat by the last known page size."""
        self.startat -= self._last_page_size

    @property
    def filter(self) -> str:
        """Get the filter value, if we should be using it."""
        if self._filter and self._filter.value:
            return self._filter.value
        return None

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        """Render table dynamically based on provided space and filtering."""
        # this function is rapidly becoming in need of breaking down :P
        # -6 for title, header, spacers, -1 for footer
        height = options.height - 7  # could also use max_height?
        width = options.max_width - 2  # could also use min_width?

        if self._filter and (self._filter.value or self._filter.editing):
            # save space for and render the filter editing pane
            height -= 1
            yield self._filter

        # saved for page increment
        self._last_page_size = height

        # compile and execute the filter
        # the filter against all columns
        def filter(df: dd.DataFrame) -> dd.DataFrame:
            try:
                filter = compile_filter(self.filter)
                filtered = filter(df)
                if not isinstance(filtered, dd.DataFrame):
                    # It's a series, to turn it into a DataFrame
                    return filtered.to_frame()
                return filtered
            except Exception:
                # if the filter doesn't compile or otherwise fails, then just directly apply
                if self.filter:
                    return df[
                        functools.reduce(
                            operator.__or__,
                            [
                                getattr(df, col)
                                .map(str)
                                .str.contains(self.filter, regex=False)
                                for col in df.columns
                            ],
                        )
                    ]
                return df

        filtered = (
            self.df
            # apply filter
            .pipe(filter)
            # start at self.column_startat
            .pipe(lambda df: df.iloc[:, self.column_startat :])  # noqa: E203
        )
        paged = list(
            itertools.islice(filtered.itertuples(), self.startat, self.startat + height)
        )

        def format(v: any) -> ConsoleRenderable:
            return (
                Text(v, no_wrap=True) if isinstance(v, str) else Pretty(v, no_wrap=True)
            )

        table = Table(expand=True, row_styles=[body_style, body_style_secondary])

        # The following code computes the number of columns we can comfortable render
        # in the space, starting at self.column_startat, before finally trimming down
        # to just those columns and then rendering.
        column_names = [" ", *filtered.columns]
        columns = [
            Column(name, _cells=[format(v) for v in values])
            for name, values in zip(column_names, zip(*paged))
        ]

        column_widths = [
            table._measure_column(console, options, column) for column in columns
        ]
        # +1 for column separator
        max_column_widths = np.array([width.maximum for width in column_widths]) + 1
        total_width = np.add.accumulate(max_column_widths)
        # np.where returns tuple of list of elements for each dimension.
        cant_render = np.where(total_width > width)[0]

        # cant_render[0], if it exists, is the first column index we don't have space for.
        if cant_render.size:  # np.ndarray
            # always render at least 1 column.
            max_column = max(cant_render[0], 1)
            # heuristic: we've got some room, render one column with overflow.
            if max_column < len(columns):
                available_width = width - total_width[max_column - 1]
                if available_width >= 30 and (available_width / max_column) >= 4:
                    max_column += 1
            column_names = column_names[:max_column]
            paged = [row[:max_column] for row in paged]

        for column_name in column_names:
            table.add_column(column_name)

        for row in paged:
            table.add_row(*map(format, row))

        yield table
        yield f"... {len(filtered)} total rows"


@dataclass
class CaptureKeyboardInput:
    """Editing callback class used to capture keyboard input to the interface."""

    prompt: Optional[str] = None
    value: str = ""
    editing: bool = False
    update: Callable[["CaptureKeyboardInput", str], bool] = lambda cap, s: True
    finalize: Callable[["CaptureKeyboardInput"], None] = lambda cap: None

    def send_character(self, ch: str) -> bool:
        """Evaluate the next character, update the "editor", and send value to update callback."""
        if ch in (BACKSPACE, CTRL_H):
            self.value = self.value[:-1]
        elif ch == CTRL_K:
            self.value = ""
        else:
            self.value += ch
        return self.update(self, self.value)

    @staticmethod
    def exit_on_return(cap: "CaptureKeyboardInput", s: str) -> bool:
        """Exit editing on return character."""
        if s.endswith("\n"):
            cap.value = cap.value.rstrip("\r\n")
            return False
        return True

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        cursor = "[white]???[/white]" if self.editing else ""
        prompt = self.prompt or ""
        yield f"{prompt}{self.value}{cursor}"


@dataclass
class Command:
    """Interface command dataclass."""

    key: str
    short_description: str
    fn: Callable
    help: str

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        """Call command"""
        return self.fn(*args, **kwds)


def add_command(command_dict: Dict, key: str, short_description: str) -> Callable:
    """Add a command to command_dict"""

    def decorator(fn: Callable) -> Callable:
        if key in command_dict:
            raise KeyError(f"Command {key} already exists")
        command_dict[key] = Command(key, short_description, fn, fn.__doc__)
        return fn

    return decorator


class Interface:
    """
    Class maintaining state for the interface.

    This class coordinates the keyboard handler with anything else stateful that it
    should interact with. It has a __rich__ method which controls how the entire
    interface is rendered.

    We need Interface to know about the df so it _can_ rerender the table if it wants,
    for instance to filter to specific rows or columns; however we also don't want
    Interface to become a big blob where we throw all of the code. Ideally Interface
    should be a "controller" and should dole out responsibility of rendering to others.
    """

    commands = {}
    table_commands = {}

    def __init__(self, df: dd.DataFrame, title: str):
        self.df = df
        self.summary = Summary(self.df)
        self.table = TableView(self.df)
        self.mode = Mode.TABLE
        self.help = Help(
            {"Mode Commands": self.commands, "Table Commands": self.table_commands}
        )
        self.editing = None

    async def keyboard_handler(self, ch: str, refresh: Callable[[], None]) -> bool:
        """
        This function is executed serially per input typed by the keyboard.

        It does not need to be thread safe; the keyboard event generator will not
        call it in parallel. `ch` will always have length 1.
        """
        # If something is capturing input, defer to it.
        if self.editing is not None:
            if ch == ESCAPE_KEY or not self.editing.send_character(ch):
                self.editing.finalize(self.editing)
                self.editing.editing = False
                self.editing = None
            refresh()
            return True

        # If the command is registered, call it
        if self.mode == Mode.TABLE:
            if ch in self.table_commands:
                return self.table_commands[ch].fn(self, refresh)

        if ch in self.commands:
            return self.commands[ch].fn(self, refresh)

        # If a command hasn't been found by this point, there isn't one defined.

        return True

    def __rich__(self) -> ConsoleRenderable:
        """Render the interface layout."""
        layout = Layout()
        layout.split(
            Layout(header, name="header", size=1),
            mode_line(self.mode),
            Layout(body, name="main"),
        )
        if self.mode == Mode.HELP:
            output = self.help
        else:
            output = self.table if self.mode == Mode.TABLE else self.summary
        padded_output = Styled(Padding(output, (1, 2)), body_style)

        layout["main"].update(padded_output)

        return layout

    @add_command(table_commands, "/", "filter")
    def edit_filter(self, refresh: Callable) -> bool:
        """
        Edit the table filter.

        Supports arbitrary text, which will do a full row search for that text (converts
        cells to strings, so eg. you can search for substrings of typed data).

        Also supports pandas-style expressions. `df` as well as each individual column
        are in the namespace; for instance, `name.isna() & salary >= salary.max() - 1e4` is a valid filter.
        """

        def _update_filter(s: str) -> bool:
            """Newline means done editing filter; otherwise update."""
            return not s.endswith("\n")

        self.editing = self.table._filter
        self.editing.editing = True
        refresh()
        return True

    # switch modes (TODO: input modes)
    @add_command(commands, "s", "(s)ummary")
    def summary_mode(self, refresh: Callable) -> bool:
        """Show a summary of the database"""
        self.mode = Mode.SUMMARY
        refresh()
        return True

    @add_command(commands, "t", "(t)able")
    def table_mode(self, refresh: Callable) -> bool:
        """Show the database as a table"""
        self.mode = Mode.TABLE
        refresh()
        return True

    # TABLE MODE: table navigation (TODO: arrow keys)
    @add_command(table_commands, "h", "scroll left")
    def scroll_left(self, refresh: Callable) -> bool:
        """Scroll left one column in the table view"""
        self.table.column_startat -= 1
        refresh()
        return True

    @add_command(table_commands, "j", "scroll down")
    def scroll_down(self, refresh: Callable) -> bool:
        """Scroll down one page in the table view"""
        self.table.increment_page()
        refresh()
        return True

    @add_command(table_commands, "k", "scroll up")
    def scroll_up(self, refresh: Callable) -> bool:
        """Scroll up one page in the table view"""
        self.table.decrement_page()
        refresh()
        return True

    @add_command(table_commands, "l", "scroll right")
    def scroll_right(self, refresh: Callable) -> bool:
        """Scroll right one column in the table view"""
        self.table.column_startat += 1
        refresh()
        return True

    @add_command(table_commands, "g", "Go to top")
    def go_to_top(self, refresh: Callable) -> bool:
        """Go to the top of the table"""
        self.table.startat = 0
        refresh()
        return True

    @add_command(table_commands, "G", "Go to bottom")
    def go_to_bottom(self, refresh: Callable) -> bool:
        """Go to the bottom of the table"""
        self.table.startat = len(self.df) - self.table._last_page_size
        refresh()
        return True

    # quit (TODO: if input is lagged, doesn't work)
    @add_command(commands, "q", "(q)uit")
    def quit(self, refresh: Callable) -> bool:
        """Quit"""
        return False

    @add_command(commands, "?", "help")
    def show_help(self, refresh: Callable) -> bool:
        """Show this help page"""
        self.mode = Mode.HELP
        refresh()
        return True
