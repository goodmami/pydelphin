
import re

from delphin.exceptions import TSQLSyntaxError
from delphin.util import LookaheadIterator, parse_datetime
from delphin import itsdb


### QUERY PROCESSING ##########################################################

def query(query, ts):
    queryobj = _parse_query(LookaheadIterator(_lex(query)))
    return queryobj


def select(query, ts):
    queryobj = _parse_select(LookaheadIterator(_lex(query)))
    projection = queryobj['projection']
    # start with 'from' tables, apply constraints, join projection
    table = _select_from(queryobj['tables'], None, ts)
    table = _select_where(queryobj['where'], table, ts)
    table = _select_projection(projection, table, ts)
    # finally select the relevant columns from the joined table
    if projection == '*':
        projection = [f.name for f in table.fields]
    return itsdb.select_rows(projection, table)


def _select_from(tables, table, ts):
    joined = set([] if table is None else table.name.split('+'))
    for tab in tables:
        if tab not in joined:
            joined.add(tab)
            table = _transitive_join(table, ts[tab], ts, 'inner')
    return table


def _select_where(condition, table, ts):
    if condition is not None:
        func, fields = _process_condition(condition)
        for field in fields:
            table = _join_if_missing(table, field, ts, 'left')
        table = itsdb.Table(
            table.name,
            table.fields,
            list(filter(func, table)))
    return table


def _process_condition(condition):
    op, body = condition
    if op == 'and':
        lfunc, lfields = _process_condition(body[0])
        rfunc, rfields = _process_condition(body[1])
        func = lambda row, lfunc=lfunc, rfunc=rfunc: lfunc(row) and rfunc(row)
        fields = lfields + rfields
    elif op == 'or':
        lfunc, lfields = _process_condition(body[0])
        rfunc, rfields = _process_condition(body[1])
        func = lambda row, lfunc=lfunc, rfunc=rfunc: lfunc(row) or rfunc(row)
        fields = lfields + rfields
    elif op == 'not':
        nfunc, fields = _process_condition(body)
        func = lambda row, nfunc=nfunc: not nfunc(row)
    else:
        col, val = body
        fields = [col]
        if op == '~':
            func = lambda row, val=val, col=col: re.search(val, row[col])
        elif op == '!~':
            func = lambda row, val=val, col=col: not re.search(val, row[col])
        elif op == '==':
            func = lambda row, val=val, col=col: row[col] == val
        elif op == '!=':
            func = lambda row, val=val, col=col: row[col] != val
        elif op == '<':
            func = lambda row, val=val, col=col: row[col] < val
        elif op == '<=':
            func = lambda row, val=val, col=col: row[col] <= val
        elif op == '>':
            func = lambda row, val=val, col=col: row[col] > val
        elif op == '>=':
            func = lambda row, val=val, col=col: row[col] >= val
    return func, fields


def _select_projection(projection, table, ts):
    if projection != '*':
        for p in projection:
            table = _join_if_missing(table, p, ts, 'inner')
    return table


def _join_if_missing(table, col, ts, how):
    tab, _, column = col.rpartition(':')
    if not tab:
        # Just get the first table defining the column. This
        # makes the assumption that relations are ordered and
        # that the first one is 'primary'
        tab = ts.relations.find(column)[0]
    if table is None or column not in table.fields:
        table = _transitive_join(table, ts[tab], ts, how)
    return table


def _transitive_join(tab1, tab2, ts, how):
    if tab1 is None:
        table = tab2
    else:
        table = tab1
        # if the tables aren't directly joinable but are joinable
        # transitively via a 'path' of table joins, do so first
        path = _id_path(tab1, tab2, ts.relations)
        for intervening, _ in path[1:]:
            table = itsdb.join(table, ts[intervening], how=how)
        # now the tables are either joinable or no path existed
        table = itsdb.join(table, tab2, how=how)
    return table


def _id_path(src, tgt, rels):
    """
    Find the path of id fields connecting two tables.

    This is just a basic breadth-first-search. The relations file
    should be small enough to not be a problem.
    """
    paths = [[(src.name, key)] for key in src.fields.keys()]
    tgtkeys = set(tgt.fields.keys())
    visited = set(src.name.split('+'))
    while True:
        newpaths = []
        for path in paths:
            laststep = path[-1]
            if laststep[1] in tgtkeys:
                return path
            else:
                for table in set(rels.find(laststep[1])) - visited:
                    visited.add(table)
                    keys = rels[table].keys()
                    if len(keys) > 1:
                        for key in rels[table].keys():
                            step = (table, key)
                            if step not in path:
                                newpaths.append(path + [step])
        if newpaths:
            paths = newpaths
        else:
            break


### QUERY PARSING #############################################################

_keywords = list(map(re.escape,
                     ('info', 'set', 'retrieve', 'select', 'insert',
                      'from', 'where', 'report', '*', '.')))
_operators = list(map(re.escape,
                      ('==', '=', '!=', '~', '!~', '<=', '<', '>=', '>',
                       '&&', '&', 'and', '||', '|', 'or', '!', 'not')))

_tsql_lex_re = re.compile(
    r'''# regex-pattern                      gid  description
    ({keywords})                           #   1  keywords
    |({operators})                         #   2  operators
    |(\(|\))                               #   3  parentheses
    |"([^"\\]*(?:\\.[^"\\]*)*)"            #   4  double-quoted "strings"
    |'([^'\\]*(?:\\.[^'\\]*)*)'            #   5  single-quoted 'strings'
    |({yyyy}-{m}(?:-{d})?(?:{t}|{tt})?)    #   6  yyyy-mm-dd date
    |((?:{d}-)?{m}-{yy}(?:{t}|{tt})?)      #   7  dd-mm-yy date
    |(:today|now)                          #   8  keyword date
    |([+-]?\d+)                            #   9  integers
    |((?:{id}:)?{id}(?:@(?:{id}:)?{id})*)  #  10  identifier (extended def)
    |([^\s])                               #  11  unexpected
    '''.format(keywords='|'.join(_keywords),
               operators='|'.join(_operators),
               d=r'[0-9]{1,2}',
               m=(r'(?:[0-9]{1,2}|'
                  r'jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)'),
               yy=r'(?:[0-9]{2})?[0-9]{2}',
               yyyy=r'[0-9]{4}',
               t=r'\s*\([0-9]{2}:[0-9]{2}(?::[0-9]{2})?\)',
               tt=r'\s+[0-9]{2}:[0-9]{2}(?::[0-9]{2})',
               id=r'[a-zA-Z][-_a-zA-Z0-9]*'),
    flags=re.VERBOSE|re.IGNORECASE)


def _lex(s):
    """
    Lex the input string according to _tsql_lex_re.

    Yields
        (gid, token, line_number)
    """
    s += '.'  # make sure there's a terminator to know when to stop parsing
    lines = enumerate(s.splitlines(), 1)
    lineno = pos = 0
    try:
        for lineno, line in lines:
            matches = _tsql_lex_re.finditer(line)
            for m in matches:
                gid = m.lastindex
                if gid == 11:
                    raise TSQLSyntaxError('unexpected input',
                                          lineno=lineno,
                                          offset=m.start(),
                                          text=line)
                else:
                    token = m.group(gid)
                    yield (gid, token, lineno)
    except StopIteration:
        pass


def _parse_query(tokens):
    gid, token, lineno = tokens.next()
    _expect(gid == 1 and token in 'info set retrieve select insert'.split(),
            token, lineno, 'a query type')
    if token not in ('retrieve', 'select'):
        raise TSQLSyntaxError("'{}' queries are not supported".format(token),
                              lineno=lineno)
    else:
        result = _parse_select(tokens)

    gid, token, lineno = tokens.next()
    _expect(gid == 2 and token == '.', token, lineno, "'.'")

    return result


def _parse_select(tokens):
    _, token, lineno = tokens.peek()  # maybe used in error below

    projection = _parse_select_projection(tokens)
    tables = _parse_select_from(tokens)
    condition = _parse_select_where(tokens)

    if projection == '*' and not tables and condition is None:
        raise TSQLSyntaxError(
            "'select *' requires a 'from' or 'where' statement",
            lineno=lineno, text=token)

    return {'querytype': 'select',
            'projection': projection,
            'tables': tables,
            'where': condition}


def _parse_select_projection(tokens):
    gid, token, lineno = tokens.next()
    if token == '*':
        projection = token
    elif gid == 10:
        projection = [token]
        while tokens.peek()[0] == 10:
            _, col, _ = tokens.next()
            projection.append(col)
        projection = _prepare_columns(projection)
    else:
        raise TSQLSyntaxError("expected '*' or column identifiers",
                              lineno=lineno, text=token)
    return projection


def _prepare_columns(cols):
    columns = []
    for col in cols:
        table = ''
        for part in col.split('@'):
            tblname, _, colname = part.rpartition(':')
            if tblname:
                table = tblname + ':'
            columns.append(table + colname)
    return columns


def _parse_select_from(tokens):
    tables = []
    if tokens.peek()[1] == 'from':
        tokens.next()
        while tokens.peek()[0] == 10:
            _, table, _ = tokens.next()
            tables.append(table)
    return tables


def _parse_select_where(tokens):
    if tokens.peek()[1] == 'where':
        tokens.next()
        condition = _parse_condition_disjunction(tokens)
    else:
        condition = None
    return condition


def _parse_condition_disjunction(tokens):
    conds = []
    while True:
        cond = _parse_condition_conjunction(tokens)
        if cond is not None:
            conds.append(cond)
        if tokens.peek()[1] in ('|', '||', 'or'):
            tokens.next()
            nextgid, nexttoken, nextlineno = tokens.peek()
        else:
            break

    if len(conds) == 0:
        return None
    elif len(conds) == 1:
        return conds[0]
    else:
        return ('or', tuple(conds))


def _parse_condition_conjunction(tokens):
    conds = []
    nextgid, nexttoken, nextlineno = tokens.peek()
    while True:
        if nextgid == 2 and nexttoken.lower() in ('!', 'not'):
            cond = _parse_condition_negation(tokens)
        elif nextgid == 3 and nexttoken == '(':
            cond = _parse_condition_group(tokens)
        elif nextgid == 3 and nexttoken == ')':
            break
        elif nextgid == 10:
            cond = _parse_condition_statement(tokens)
        else:
            raise TSQLSyntaxError("expected '!', 'not', '(', or a column name",
                                  lineno=nextlineno, text=nexttoken)
        conds.append(cond)
        if tokens.peek()[1].lower() in ('&', '&&', 'and'):
            tokens.next()
            nextgid, nexttoken, nextlineno = tokens.peek()
        else:
            break

    if len(conds) == 0:
        return None
    elif len(conds) == 1:
        return conds[0]
    else:
        return ('and', tuple(conds))


def _parse_condition_negation(tokens):
    gid, token, lineno = tokens.next()
    _expect(gid == 2 and token in ('!', 'not'), token, lineno, "'!' or 'not'")
    cond = _parse_condition_disjunction(tokens)
    return ('not', cond)


def _parse_condition_group(tokens):
    gid, token, lineno = tokens.next()
    _expect(gid == 3 and token == '(', token, lineno, "'('")
    cond = _parse_condition_disjunction(tokens)
    gid, token, lineno = tokens.next()
    _expect(gid == 3 and token == ')', token, lineno, "')'")
    return tuple(cond)


def _parse_condition_statement(tokens):
    gid, column, lineno = tokens.next()
    _expect(gid == 10, column, lineno, 'a column name')
    gid, op, lineno = tokens.next()
    _expect(gid == 2, op, lineno, 'an operator')
    if op == '=':
        op = '=='  # normalize = to == (I think these are equivalent)
    gid, value, lineno = tokens.next()
    if op in ('~', '!~') and gid not in (4, 5):
        raise TSQLSyntaxError(
            "the '{}' operator is only valid with strings".format(op),
            lineno=lineno, text=op)
    elif op in ('<', '<=', '>', '>=') and gid not in (6, 7, 8, 9):
        raise TSQLSyntaxError(
            "the '{}' operator is only valid with integers and dates"
            .format(op), lineno=lineno, text=op)
    else:
        if gid in (6, 7, 8):
            value = parse_datetime(value)
        elif gid == 9:
            value = int(value)
        return (op, (column, value))


def _expect(expected, token, lineno, msg):
    msg = 'expected ' + msg
    if not expected:
        raise TSQLSyntaxError(msg, lineno=lineno, text=token)
