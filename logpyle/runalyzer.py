#! /usr/bin/env python

import code

try:
    import readline
    import rlcompleter  # noqa: F401
    HAVE_READLINE = True
except ImportError:
    HAVE_READLINE = False


import logging
from typing import Tuple

logger = logging.getLogger(__name__)

from dataclasses import dataclass
from itertools import product


@dataclass(frozen=True)
class PlotStyle:
    dashes: Tuple[int, ...]
    color: str


PLOT_STYLES = [
        PlotStyle(dashes=dashes, color=color)
        for dashes, color in product(
            [(), (12, 2), (4, 2),  (2, 2), (2, 8)],
            ["blue", "green", "red", "magenta", "cyan"],
            )]


class RunDB:
    def __init__(self, db, interactive):
        self.db = db
        self.interactive = interactive
        self.rank_agg_tables = set()

    def q(self, qry, *extra_args):
        return self.db.execute(self.mangle_sql(qry), extra_args)

    def mangle_sql(self, qry):
        return qry

    def get_rank_agg_table(self, qty, rank_aggregator):
        tbl_name = f"rankagg_{rank_aggregator}_{qty}"

        if (qty, rank_aggregator) in self.rank_agg_tables:
            return tbl_name

        logger.info("Building temporary rank aggregation table {tbl_name}.")

        self.db.execute("create temporary table %s as "
                "select run_id, step, %s(value) as value "
                "from %s group by run_id,step" % (
                    tbl_name, rank_aggregator, qty))
        self.db.execute("create index %s_run_step on %s (run_id,step)"
                % (tbl_name, tbl_name))
        self.rank_agg_tables.add((qty, rank_aggregator))
        return tbl_name

    def scatter_cursor(self, cursor, labels=None, *args, **kwargs):
        import matplotlib.pyplot as plt

        data_args = tuple(zip(*list(cursor)))
        plt.scatter(*(data_args + args), **kwargs)

        if isinstance(labels, list) and len(labels) == 2:
            plt.xlabel(labels[0])
            plt.ylabel(labels[1])
        elif labels is not None:
            raise TypeError("The 'labels' parameter must be a list with two"
                            "elements.")

        if self.interactive:
            plt.show()

    def plot_cursor(self, cursor, labels=None, *args, **kwargs):
        from matplotlib.pyplot import legend, plot, show

        auto_style = kwargs.pop("auto_style", True)

        if len(cursor.description) == 2:
            if auto_style:
                style = PLOT_STYLES[0]
                kwargs["dashes"] = style.dashes
                kwargs["color"] = style.color

            x, y = list(zip(*list(cursor)))
            p = plot(x, y, *args, **kwargs)

            if isinstance(labels, list) and len(labels) == 2:
                p[0].axes.set_xlabel(labels[0])
                p[0].axes.set_ylabel(labels[1])
            elif labels is not None:
                raise TypeError("The 'labels' parameter must be a list with two"
                                " elements.")

        elif len(cursor.description) > 2:
            small_legend = kwargs.pop("small_legend", True)

            def format_label(kv_pairs):
                return " ".join(f"{column}:{value}"
                            for column, value in kv_pairs)
            format_label = kwargs.pop("format_label", format_label)

            def do_plot(x, y, row_rest):
                my_kwargs = kwargs.copy()
                style = PLOT_STYLES[style_idx[0] % len(PLOT_STYLES)]
                if auto_style:
                    my_kwargs.setdefault("dashes", style.dashes)
                    my_kwargs.setdefault("color", style.color)

                my_kwargs.setdefault("label",
                        format_label(list(zip(
                            (col[0] for col in cursor.description[2:]),
                            row_rest))))

                plot(x, y, *args, hold=True, **my_kwargs)
                style_idx[0] += 1

            style_idx = [0]
            for x, y, rest in split_cursor(cursor):
                do_plot(x, y, rest)

            if small_legend:
                from matplotlib.font_manager import FontProperties
                legend(pad=0.04, prop=FontProperties(size=8), loc="best",
                        labelsep=0)
        else:
            raise ValueError("invalid number of columns")

        if self.interactive:
            show()

    def print_cursor(self, cursor):
        print(table_from_cursor(cursor))


def split_cursor(cursor):
    x = []
    y = []
    last_rest = None
    for row in cursor:
        row_tuple = tuple(row)
        row_rest = row_tuple[2:]

        if last_rest is None:
            last_rest = row_rest

        if row_rest != last_rest:
            yield x, y, last_rest
            del x[:]
            del y[:]

            last_rest = row_rest

        x.append(row_tuple[0])
        y.append(row_tuple[1])
    if x:
        yield x, y, last_rest


def table_from_cursor(cursor):
    from pytools import Table
    tbl = Table()
    tbl.add_row([column[0] for column in cursor.description])
    for row in cursor:
        tbl.add_row(row)
    return tbl


class MagicRunDB(RunDB):
    def mangle_sql(self, qry):
        up_qry = qry.upper()
        if "FROM" in up_qry and "$$" not in up_qry:
            return qry

        magic_columns = set()

        def replace_magic_column(match):
            qty_name = match.group(1)
            rank_aggregator = match.group(2)

            if rank_aggregator is not None:
                rank_aggregator = rank_aggregator[1:]
                magic_columns.add((qty_name, rank_aggregator))
                return f"{rank_aggregator}_{qty_name}.value AS {qty_name}"
            else:
                magic_columns.add((qty_name, None))
                return "%s.value AS %s" % (qty_name, qty_name)

        import re
        magic_column_re = re.compile(r"\$([a-zA-Z][A-Za-z0-9_]*)(\.[a-z]*)?")
        qry, _ = magic_column_re.subn(replace_magic_column, qry)

        other_clauses = [  # noqa: F841
                "UNION",  "INTERSECT", "EXCEPT", "WHERE", "GROUP",
                "HAVING", "ORDER", "LIMIT", ";"]

        from_clause = "from runs "
        last_tbl = None
        for tbl, rank_aggregator in magic_columns:
            if rank_aggregator is not None:
                full_tbl = f"{rank_aggregator}_{tbl}"
                full_tbl_src = "{} as {}".format(
                        self.get_rank_agg_table(tbl, rank_aggregator),
                        full_tbl)

                if last_tbl is not None:
                    addendum = f" and {last_tbl}.step = {full_tbl}.step"
                else:
                    addendum = ""
            else:
                full_tbl = tbl
                full_tbl_src = tbl

                if last_tbl is not None:
                    addendum = " and {}.step = {}.step and {}.rank={}.rank".format(
                            last_tbl, full_tbl, last_tbl, full_tbl)
                else:
                    addendum = ""

            from_clause += " inner join {} on ({}.run_id = runs.id{}) ".format(
                    full_tbl_src, full_tbl, addendum)
            last_tbl = full_tbl

        def get_clause_indices(qry):
            other_clauses = ["UNION",  "INTERSECT", "EXCEPT", "WHERE", "GROUP",
                    "HAVING", "ORDER", "LIMIT", ";"]

            result = {}
            up_qry = qry.upper()
            for clause in other_clauses:
                clause_match = re.search(r"\b%s\b" % clause, up_qry)
                if clause_match is not None:
                    result[clause] = clause_match.start()

            return result

        # add 'from'
        if "$$" in qry:
            qry = qry.replace("$$", " %s " % from_clause)
        else:
            clause_indices = get_clause_indices(qry)

            if not clause_indices:
                qry = qry+" "+from_clause
            else:
                first_clause_idx = min(clause_indices.values())
                qry = (
                        qry[:first_clause_idx]
                        + from_clause
                        + qry[first_clause_idx:])

        return qry


def make_runalyzer_symbols(db):
    return {
            "__name__": "__console__",
            "__doc__": None,
            "db": db,
            "mangle_sql": db.mangle_sql,
            "q": db.q,
            "dbplot": db.plot_cursor,
            "dbscatter": db.scatter_cursor,
            "dbprint": db.print_cursor,
            "split_cursor": split_cursor,
            "table_from_cursor": table_from_cursor,
            }


class RunalyzerConsole(code.InteractiveConsole):
    def __init__(self, db):
        self.db = db
        code.InteractiveConsole.__init__(self,
                make_runalyzer_symbols(db))

        try:
            import numpy  # noqa: F401
            self.runsource("from numpy import *")
        except ImportError:
            pass

        try:
            import matplotlib.pyplot  # noqa
            self.runsource("from matplotlib.pyplot import *")
        except ImportError:
            pass
        except RuntimeError:
            pass

        if HAVE_READLINE:
            import atexit
            import os

            histfile = os.path.join(os.environ["HOME"], ".runalyzerhist")
            if os.access(histfile, os.R_OK):
                readline.read_history_file(histfile)
            atexit.register(readline.write_history_file, histfile)
            readline.parse_and_bind("tab: complete")

        self.last_push_result = False

    def push(self, cmdline):
        if cmdline.startswith("."):
            try:
                self.execute_magic(cmdline)
            except Exception:
                import traceback
                traceback.print_exc()
        else:
            self.last_push_result = code.InteractiveConsole.push(self, cmdline)

        return self.last_push_result

    def execute_magic(self, cmdline):
        cmd_end = cmdline.find(" ")
        if cmd_end == -1:
            cmd = cmdline[1:]
            args = ""
        else:
            cmd = cmdline[1:cmd_end]
            args = cmdline[cmd_end+1:]

        if cmd == "help":
            print("""
Commands:
 .help        show this help message
 .q SQL       execute a (potentially mangled) query
 .runprops    show a list of run properties
 .quantities  show a list of time-dependent quantities

Plotting:
 .plot SQL    plot results of (potentially mangled) query.
              result sets can be (x,y) or (x,y,descr1,descr2,...),
              in which case a new plot will be started for each
              tuple (descr1, descr2, ...)
 .scatter SQL make scatterplot results of (potentially mangled) query.
              result sets can have between two and four columns
              for (x,y,size,color).

SQL mangling, if requested ("MagicSQL"):
    select $quantity where pred(feature)

Custom SQLite aggregates:
    stddev, var, norm1, norm2

Available Python symbols:
    db: the SQLite database
    mangle_sql(query_str): mangle the SQL query string query_str
    q(query_str): get db cursor for mangled query_str
    dbplot(cursor): plot result of cursor
    dbscatter(cursor): make scatterplot result of cursor
    dbprint(cursor): print result of cursor
    split_cursor(cursor): x,y,data gather that .plot uses internally
    table_from_cursor(cursor)
""")
        elif cmd == "q":
            self.db.print_cursor(self.db.q(args))

        elif cmd == "runprops":
            cursor = self.db.db.execute("select * from runs")
            columns = [column[0] for column in cursor.description]
            columns.sort()
            for col in columns:
                print(col)
        elif cmd == "quantities":
            self.db.print_cursor(self.db.q("select * from quantities order by name"))
        elif cmd == "title":
            from pylab import title
            title(args)
        elif cmd == "plot":
            cursor = self.db.db.execute(self.db.mangle_sql(args))
            columnnames = [column[0] for column in cursor.description]
            self.db.plot_cursor(cursor, labels=columnnames)
        elif cmd == "scatter":
            cursor = self.db.db.execute(self.db.mangle_sql(args))
            columnnames = [column[0] for column in cursor.description]
            self.db.scatter_cursor(cursor, labels=columnnames)
        else:
            print("invalid magic command")


# {{{ custom aggregates

from pytools import VarianceAggregator  # noqa: E402


class Variance(VarianceAggregator):
    def __init__(self):
        VarianceAggregator.__init__(self, entire_pop=True)


class StdDeviation(Variance):
    def finalize(self):
        result = Variance.finalize(self)

        if result is None:
            return None
        else:
            from math import sqrt
            return sqrt(result)


class Norm1:
    def __init__(self):
        self.abs_sum = 0

    def step(self, value):
        self.abs_sum += abs(value)

    def finalize(self):
        return self.abs_sum


class Norm2:
    def __init__(self):
        self.square_sum = 0

    def step(self, value):
        self.square_sum += value**2

    def finalize(self):
        from math import sqrt
        return sqrt(self.square_sum)


def my_sprintf(format, arg):
    return format % arg

# }}}


# {{{ main program

def make_wrapped_db(filename, interactive, mangle):
    import sqlite3
    db = sqlite3.connect(filename)
    db.create_aggregate("stddev", 1, StdDeviation)
    db.create_aggregate("var", 1, Variance)
    db.create_aggregate("norm1", 1, Norm1)
    db.create_aggregate("norm2", 1, Norm2)

    db.create_function("sprintf", 2, my_sprintf)
    from math import pow, sqrt
    db.create_function("sqrt", 1, sqrt)
    db.create_function("pow", 2, pow)

    if mangle:
        db_wrap_class = MagicRunDB
    else:
        db_wrap_class = RunDB

    return db_wrap_class(db, interactive=interactive)

# }}}

# vim: foldmethod=marker
