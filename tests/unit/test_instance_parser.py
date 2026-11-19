import textwrap

from ripple.graph.schema import ParsedFile
from ripple.parsers.python_parser import parse_python_source

PATH = "app/mod.py"


def parse(src: str):
    return parse_python_source(textwrap.dedent(src).lstrip("\n"), PATH)


def inst(result):
    return sorted((i.scope_id, i.var, i.ctor, i.line) for i in result.instances)


def test_module_level_instances():
    r = parse("""
        limiter = RateLimiter(limit=60)
        settings = config.Settings()
        value = make()
        n = 5
        label = "x"
        copy = other
        """)
    assert inst(r) == [
        (PATH, "limiter", "RateLimiter", 1),
        (PATH, "settings", "config.Settings", 2),
        (PATH, "value", "make", 3),
    ]
    assert r.bound == {}


def test_annotated_assignment_counts_and_bare_annotation_does_not():
    r = parse("""
        limiter: RateLimiter = RateLimiter()
        pending: Cache
        """)
    assert inst(r) == [(PATH, "limiter", "RateLimiter", 1)]


def test_names_bound_more_than_once_are_dropped():
    r = parse("""
        a = A()
        a = B()

        if FLAG:
            b = A()
        else:
            b = B()

        c = A()

        def c(): ...

        d = A()
        for d in items: ...
        """)
    assert inst(r) == []


def test_a_single_conditional_or_try_assignment_is_kept():
    r = parse("""
        if DEBUG:
            tracer = Tracer()

        try:
            client = Client()
        except ImportError:
            pass
        """)
    assert inst(r) == [(PATH, "client", "Client", 5), (PATH, "tracer", "Tracer", 2)]


def test_global_rebinding_inside_a_function_drops_the_module_instance():
    r = parse("""
        cache = Cache()
        other = Other()

        def reset():
            global cache
            cache = None
        """)
    assert inst(r) == [(PATH, "other", "Other", 2)]
    assert r.bound == {}


def test_function_locals_are_scoped_to_the_function():
    r = parse("""
        def f(builder, n=1, *rest, **opts):
            b = GraphBuilder()
            c = models.Thing()
            c = other()
            d = 1
            return b
        """)
    f = f"{PATH}::f"
    assert inst(r) == [(f, "b", "GraphBuilder", 2)]
    assert r.bound == {f: ["b", "builder", "c", "d", "n", "opts", "rest"]}


def test_a_parameter_that_is_reassigned_is_not_an_instance():
    r = parse("""
        def f(settings):
            settings = Settings()
        """)
    assert inst(r) == []
    assert r.bound == {f"{PATH}::f": ["settings"]}


def test_nested_functions_have_their_own_scopes():
    r = parse("""
        def outer():
            a = A()

            def inner():
                a = B()
        """)
    assert inst(r) == [
        (f"{PATH}::outer", "a", "A", 2),
        (f"{PATH}::outer.inner", "a", "B", 5),
    ]
    assert r.bound == {
        f"{PATH}::outer": ["a", "inner"],
        f"{PATH}::outer.inner": ["a"],
    }


def test_class_bodies_are_not_scopes_but_methods_are():
    r = parse("""
        class C:
            shared = Cache()

            def m(self):
                local = Cache()
        """)
    assert inst(r) == [(f"{PATH}::C.m", "local", "Cache", 5)]
    assert r.bound == {f"{PATH}::C.m": ["local", "self"]}


def test_every_binding_form_counts():
    r = parse("""
        def f(items):
            import json
            import os.path
            from a import b as c
            for x in items:
                pass
            with open("f") as fh:
                pass
            try:
                pass
            except OSError as err:
                pass
            if (n := len(items)):
                pass
            def helper(): ...
            class Local: ...
        """)
    assert r.bound[f"{PATH}::f"] == ["Local", "err", "fh", "helper", "items", "n", "x"]


def test_match_captures_count():
    r = parse("""
        def f(v):
            match v:
                case [a, *rest]:
                    pass
                case {"k": 1, **kw}:
                    pass
                case Point(x=px) as p:
                    pass
        """)
    assert r.bound[f"{PATH}::f"] == ["a", "kw", "p", "px", "rest", "v"]


def test_only_plain_name_targets_are_candidates():
    r = parse("""
        def f(self):
            a = b = A()
            c, d = A(), B()
            self.x = A()
        """)
    assert inst(r) == []
    assert r.bound == {f"{PATH}::f": ["a", "b", "c", "d", "self"]}


def test_syntax_error_has_no_instances():
    r = parse("def broken(:\n")
    assert r.instances == [] and r.bound == {}


def test_new_fields_default_to_empty():
    pf = ParsedFile(path="x.py")
    assert pf.instances == [] and pf.bound == {}


def test_function_local_imports_do_not_count_as_shadowing():
    r = parse("""
        def f():
            import json
            from a import b as c
        """)
    assert r.bound == {}


def test_an_imported_and_assigned_name_is_not_an_instance():
    r = parse("""
        def f():
            from m import Thing
            Thing = Thing()
        """)
    assert inst(r) == []
    assert r.bound == {f"{PATH}::f": ["Thing"]}
