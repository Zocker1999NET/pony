from compiler import ast
from types import NoneType

from pony import orm
from pony.decompiler import decompile
from pony.templating import Html, StrHtml
from pony.dbapiprovider import SQLBuilder
from pony.sqlsymbols import *

def walk(node):
    for child in node.getChildNodes():
        for subchild in walk(child):
            yield subchild
    yield node

primitive_types = set([ int, unicode ])
type_normalization_dict = { long : int, str : unicode, StrHtml : unicode, Html : unicode }

def normalize_type(t):
    if t is NoneType: return t
    t = type_normalization_dict.get(t, t)
    if t not in primitive_types and not isinstance(t, orm.EntityMeta): raise TypeError, t
    return t

def is_comparable_types(op, type1, type2):
    # op: '<' | '>' | '=' | '>=' | '<=' | '<>' | '!=' | '=='
    #         | 'in' | 'not' 'in' | 'is' | 'is' 'not'
    if op in ('is', 'is not'): return type2 is NoneType
    if op in ('<', '<=', '>', '>='): return type1 is type2 and type1 in primitive_types
    if op in ('==', '<>', '!='):
        if type1 is NoneType or type2 is NoneType: return True
        elif type1 in primitive_types: return type1 is type2
        elif isinstance(type1, orm.EntityMeta): return type1._root_ is type2._root_
        else: return False
    if op in ['in', 'not in']:
        if type1 in primitive_types:            
            if type(type2) in (tuple, list): return set(type2) == set((type1,))         
            elif isinstance(type2, orm.Set): return type1 is type2.py_type
            else: return False
        elif isinstance(type1, orm.EntityMeta):
            if type(type2) in (tuple, list):                
                for t in type2:
                    if not isinstance(t, orm.EntityMeta) or type1._root_ is not t._root_: return False
                return True
            elif isinstance(type2, orm.Set):
                t = type2.py_type
                return isinstance(t, orm.EntityMeta) or type1._root_ is not t._root_
            else: return False
        else: return False

def annotate(gen):
    a = Annotator(gen)
    return a.tree

class Annotator(object):
    def __init__(self, gen):
        self.gen = gen
        self.tree = tree = decompile(gen).code
        self.itertypes = {}
        self.vartypes = {}
        assert isinstance(tree, ast.GenExprInner)
        for for_ in tree.quals:
            assign = for_.assign
            assert isinstance(assign, ast.AssName)
            assert assign.flags == 'OP_ASSIGN'
            iter_type = self.annotate(for_.iter)
            assert assign.name not in self.itertypes
            if not isinstance(iter_type, orm.EntityIter): raise NotImplementedError
            self.itertypes[assign.name] = assign.type = iter_type.entity
            for if_ in for_.ifs:
                assert isinstance(if_, ast.GenExprIf)
                if_.type = self.annotate(if_.test)                    
        tree.expr.type = self.annotate(tree.expr)
        tree.itertypes = self.itertypes
        tree.vartypes = self.vartypes
    def annotate(self, node):
        for subnode in walk(node): self.process(subnode)
        return node.type
    def process(self, node):
        method = getattr(self, 'process_' + node.__class__.__name__)
        method(node)
    def process_Name(self, node):
        name = node.name
        try: t = self.itertypes[name]
        except KeyError:
            try: t = self.vartypes[name]
            except KeyError:
                try: val = self.gen.gi_frame.f_locals[name]
                except KeyError: val = self.gen.gi_frame.f_globals[name]  # can raise KeyError
                if isinstance(val, orm.EntityIter): t = val
                elif isinstance(val, orm.EntityMeta): t = orm.EntityIter(val)
                else: t = self.vartypes[name] = normalize_type(type(val))
        node.type = t
    def process_Getattr(self, node):
        expr_type = node.expr.type
        assert isinstance(expr_type, orm.EntityMeta), expr_type
        attr_name = node.attrname
        attr = getattr(expr_type, attr_name)
        assert isinstance(attr, orm.Attribute)
        if not isinstance(attr, orm.Collection): node_type = attr.py_type
        elif isinstance(attr, orm.Set): node_type = attr
        else: raise NotImplementedError
        node.type = node_type
    def process_Const(self, node):
        const_type = type(node.value)
        if const_type is tuple: const_type = tuple(map(normalize_type, map(type, node.value)))
        elif const_type is NoneType: pass
        elif isinstance(const_type, orm.EntityMeta): pass
        else: const_type = normalize_type(type(node.value))
        node.type = const_type
    def process_Compare(self, node):
        if len(node.ops) != 1: raise NotImplementedError
        type1 = node.expr.type
        op, expr2 = node.ops[0]
        type2 = expr2.type
        if not is_comparable_types(op, type1, type2):
            raise TypeError
        node.type = bool
    def process_List(self, node):
        node.type = []
        for n in node.nodes:
            if isinstance(n, ast.Name): node.type.append(normalize_type(n.type))
            elif isinstance(n, ast.Const): node.type.append(normalize_type(n.value))
            else: raise NotImplementedError
    def process_Tuple(self, node):
        self.process_List(node)
        node.type = tuple(node.type)
    def process_And(self, node):
        for n in node.nodes:
            if n.type is not bool: raise TypeError
        node.type = bool
    process_Or = process_And
    def process_Not(self, node):
        if node.expr.type is not bool: raise TypeError
        node.type = bool

def build_query(gen):
    tree = annotate(gen)
    vars = {}
    for name, type in tree.vartypes.items():
        try: val = gen.gi_frame.f_locals[name]
        except KeyError: val = gen.gi_frame.f_globals[name]  # can raise KeyError
        vars[name] = val
    builder = QueryBuilder(tree, vars)
    return builder.sql, builder.params

cmpops = { '==' : EQ, '!=' : NE, '>=' : GE, '>' : GT, '<=' : LE, '<' : LT }        

class QueryBuilder(object):
    def __init__(self, tree, vars):
        self.tree = tree
        self.vars = vars
        self.params = {}
        self.build_select()
        self.build_from()
        self.build_where()
        self.build_query()
    def build_query(self):
        self.sql = [ SELECT, self.select, self.from_ ]
        if self.where: self.sql.append(self.where)
    def build_select(self):
        select = self.select = [ ALL ]
        expr = self.tree.expr
        entity = expr.type
        if not isinstance(expr, ast.Name): raise TypeError
        if not isinstance(entity, orm.EntityMeta): raise TypeError
        for attr in entity._attrs_:
            if isinstance(attr, orm.Collection): continue
            select.append([ COLUMN, expr.name, attr.name ])
    def build_from(self):
        from_ = self.from_ = [ FROM ]
        for qual in self.tree.quals:
            if not isinstance(qual.iter, ast.Name): raise TypeError
            assign = qual.assign
            name, type = assign.name, assign.type
            if not isinstance(type, orm.EntityMeta): raise TypeError, type
            from_.append([name, TABLE, type.__name__])
    def build_where(self):
        criteria = []
        for qual in self.tree.quals:
            for if_ in qual.ifs:
                test = if_.test                
                if test.type is not bool: raise TypeError
                criteria.append(self.build_expr(test))                
        if not criteria: self.where = []
        elif len(criteria) == 1: self.where = [ WHERE, criteria[0] ]
        else: self.where = [ WHERE, AND, criteria ]
    def build_expr(self, expr):
        method = getattr(self, 'expr_' + expr.__class__.__name__)
        return method(expr)
    def expr_And(self, node):
        return [ AND ] + [ self.build_expr(n) for n in node.nodes ]
    def expr_Or(self, node):
        return [ OR ] + [ self.build_expr(n) for n in node.nodes ]
    def expr_Not(self, node):
        return [ NOT, self.build_expr(node.expr) ]
    def expr_Compare(self, node):
        criteria = []
        a = node.expr
        for op, b in node.ops:
            if op in ('is', 'is not'):
                sqlop = op == 'is' and IS_NULL or IS_NOT_NULL
                assert isinstance(b, ast.Const) and b.value is None
                expr_a = self.build_expr(a)
                if not isinstance(expr_a, Composite): criteria.append([ sqlop, expr_a ])
                else:
                    for item in expr_a.items: criteria.append([ sqlop, item ])
            elif op in ('in', 'not in'):
                expr_a = self.build_expr(a)
                if isinstance(expr_a, Composite):
                    if not isinstance(b, (ast.List, ast.Tuple)): raise TypeError
                    orlist = [ OR ]
                    for node in b.nodes:                            
                        if not isinstance(node, ast.Name): raise TypeError
                        composite = self.expr_Name(node)
                        assert isinstance(composite, Composite)
                        andlist = [ AND ]
                        for a_item, b_item in zip(expr_a.items, composite.items):
                            andlist.append([ EQ, a_item, b_item ])
                        orlist.append(andlist)
                    criteria.append(orlist)
                else:
                    if isinstance(b, ast.Const):
                        if not isinstance(b.value, tuple): raise TypeError
                        items = [ [ VALUE, item ] for item in b.value ] 
                    elif isinstance(b, (ast.List, ast.Tuple)):
                        items = []
                        for node in b.nodes:
                            if not isinstance(node, (ast.Const, ast.Name)): raise TypeError
                            items.append(self.build_expr(node))
                    else: raise TypeError
                    criteria.append([ op == 'in' and IN or NOT_IN, self.build_expr(a), items ])
            elif op in cmpops:
                expr_a = self.build_expr(a)
                expr_b = self.build_expr(b)
                if not isinstance(expr_a, Composite) and not isinstance(expr_b, Composite):
                    if b.type is NoneType:
                        if op == '==': criteria.append([ IS_NULL, expr_a ])
                        elif op in ('!=', '<>'): criteria.append([ IS_NOT_NULL, expr_a ])
                        else: raise TypeError
                    elif a.type is NoneType:
                        if op == '==': criteria.append([ IS_NULL, expr_b ])
                        elif op in ('!=', '<>'): criteria.append([ IS_NOT_NULL, expr_b ])
                        else: raise TypeError
                    else: criteria.append([ cmpops[op], expr_a, expr_b ])
                elif a.type is NoneType or b.type is NoneType:
                    items = b.type is NoneType and expr_a.items or expr_b.items
                    if op == '==': sqlop = IS_NULL
                    elif op in ('!=', '<>'): sqlop = IS_NOT_NULL
                    else: TypeError
                    for item in items: criteria.append([ sqlop, item ])
                else:
                    if not isinstance(expr_b, Composite): raise TypeError
                    assert len(expr_a.items) != len(expr_b.items)
                    if op == '==':
                        for a_item, b_item in zip(expr_a.items, expr_b.items):
                            criteria.append([ EQ, a_item, b_item ])
                    elif op in ('!=', '<>'):
                        orlist = [ OR ]
                        for a_item, b_item in zip(expr_a.items, expr_b.items):
                            orlist.append([ NE, a_item, b_item ])
                        criteria.append(orlist)
                    else: raise TypeError
            else: assert False
            a = b
        if len(criteria) == 1: return criteria[0]
        return [ AND ] + criteria
    def expr_Const(self, node):
        return [ VALUE, node.value ]
    def expr_Getattr(self, node):
        expr = node.expr
        attrname = node.attrname
        if not isinstance(expr.type, orm.EntityMeta): raise NotImplementedError
        if not isinstance(expr, ast.Name): raise NotImplementedError
        return [ COLUMN, expr.name, attrname ]
    def expr_Name(self, node):
        type = node.type
        name = node.name
        if type is NoneType:
            return [ VALUE, None ]
        elif not isinstance(type, orm.EntityMeta):
            val = self.vars[name]
            self.params[name] = val
            return [ PARAM, node.name ]
            
        key = type._pk_attrs_
        if len(key) == 1:
            if name in self.tree.itertypes:
                return [ COLUMN, node.name, key[0].name ]
            elif name in self.vars:
                obj = self.vars[name]
                self.params[name] = obj._pkval_[0]
                return  [ PARAM, name ]
            else: assert False
        elif len(key) > 1:
            if name in self.tree.itertypes:
                return Composite.from_entity(type, name)
            elif name in self.vars:
                obj = self.vars[name]
                return Composite.from_obj(obj, name, self.params)
            else: assert False
        else: assert False

class Composite(object):
    def __init__(self, items):
        self.items = items
    @staticmethod
    def from_entity(entity, alias):
        assert len(entity._pk_attrs_) > 1
        items = []
        for attr, column in entity._expanded_pkattrs_:
            items.append([ COLUMN, alias, column ])
        return Composite(items)
    @staticmethod
    def from_obj(obj, varname, params):
        assert len(obj._pk_attrs_) > 1
        items = []
        for (attr, name), val in zip(obj._expanded_pkattrs_, obj._expand_pkval_()):
            pname = varname + '_' + name
            params[pname] = val
            items.append([ PARAM, pname ])
        return Composite(items)

def select(gen):
    sql_ast, params = build_query(gen)    
    print SQLBuilder(sql_ast).sql       
    print params
    print sql_ast
