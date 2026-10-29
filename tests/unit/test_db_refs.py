import textwrap

from ripple.parsers.db_parser import parse_db_source, parse_refs_source
from ripple.parsers.python_parser import parse_python_source

PATH = "app/repo.py"


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def refs(text: str, *heads: str):
    """(owner, ref, access, called), restricted to refs whose head is in `heads`."""
    heads = heads or ("User", "Order")
    return [
        (r.owner_id.partition("::")[2], r.ref, r.access, r.called)
        for r in parse_refs_source(src_of(text), PATH)
        if r.ref.partition(".")[0] in heads
    ]


# reads
def test_reads_through_query_select_join_and_column_attributes():
    assert refs("""
        def f(s):
            s.query(User).all()
            stmt = select(User).join(Order)
            ok = User.id == 1
        """) == [
        ("f", "User", "read", False),
        ("f", "Order", "read", False),
        ("f", "User.id", "read", False),
    ]


def test_qualified_and_aliased_chains_are_captured_whole():
    assert refs(
        """
        def f():
            a = models.User
            b = app.models.User.id
        """,
        "models",
        "app",
    ) == [
        ("f", "models.User", "read", False),
        ("f", "app.models.User.id", "read", False),
    ]


def test_calls_are_flagged_but_not_classified_here():
    assert refs("""
        def f(s):
            s.add(User(name="x"))
            User.get(1)
        """) == [
        ("f", "User", "read", True),
        ("f", "User.get", "read", True),
    ]


# writes
def test_core_write_constructs_mark_their_model_argument():
    assert refs("""
        def f(s):
            s.execute(insert(User).values(x=1))
            s.execute(update(Order).where(Order.id == 1))
            s.execute(sa.delete(User))
            s.bulk_insert_mappings(Order, rows)
            s.bulk_update_mappings(Order, rows)
        """) == [
        ("f", "User", "write", False),
        ("f", "Order", "write", False),
        ("f", "Order.id", "read", False),
    ]


def test_query_chains_ending_in_update_or_delete_are_writes():
    assert refs("""
        def f(s):
            s.query(User).filter(User.id == 1).delete()
            s.query(Order).update({"x": 1})
        """) == [
        ("f", "User", "write", False),
        ("f", "User.id", "read", False),
        ("f", "Order", "write", False),
    ]


def test_query_chain_without_update_or_delete_stays_read():
    assert refs("""
        def f(s):
            s.query(User).filter(User.id == 1).first()
            s.delete(obj)
        """) == [("f", "User", "read", False), ("f", "User.id", "read", False)]


# scoping
def test_decorators_defaults_annotations_and_returns_are_not_scanned():
    assert (
        refs("""
        @router.get("/", response_model=User)
        def f(u: User, x=Order) -> User:
            y: User = 1
        """)
        == []
    )


def test_module_and_class_bodies_are_not_scanned_but_methods_are():
    assert refs("""
        x = select(User)

        class Holder:
            fk = ForeignKey(User.id)

            def m(self):
                return User
        """) == [("Holder.m", "User", "read", False)]


def test_class_inside_function_skips_its_body_but_not_its_methods():
    assert refs("""
        def factory():
            class Local:
                x = User.id

                def m(self):
                    return User
            return Local
        """) == [("factory.Local.m", "User", "read", False)]


def test_nested_function_owns_its_refs():
    assert refs("""
        def outer():
            def inner():
                return User
            return Order
        """) == [
        ("outer.inner", "User", "read", False),
        ("outer", "Order", "read", False),
    ]


def test_comprehensions_and_lambdas_belong_to_the_enclosing_function():
    assert refs("""
        def f(xs):
            return [User(x) for x in xs], (lambda: Order)
        """) == [("f", "User", "read", True), ("f", "Order", "read", False)]


def test_self_and_cls_heads_are_dropped():
    assert refs(
        """
        class R:
            def m(self):
                self.x = User
                return self.y
        """,
        "self",
        "User",
    ) == [("R.m", "User", "read", False)]


def test_store_context_names_are_not_refs():
    assert refs("def f():\n    User = make()\n") == []


# dedupe, bundle, robustness
def test_repeated_refs_dedupe_and_keep_the_first_line():
    (r,) = [
        r
        for r in parse_refs_source(
            src_of("def f():\n    a = User\n    b = User\n"), PATH
        )
        if r.ref == "User"
    ]
    assert r.line == 2


def test_syntax_error_yields_no_refs():
    assert parse_refs_source("x = User\ndef broken(:\n", PATH) == []


def test_bundle_carries_refs_and_owner_ids_match_python_parser_nodes():
    text = src_of("""
        def top():
            return User

        class C:
            def m(self):
                def inner():
                    return Order
        """)
    node_ids = {n.id for n in parse_python_source(text, PATH).nodes}
    found = parse_db_source(text, PATH).refs
    assert {r.ref for r in found} >= {"User", "Order"}
    assert {r.owner_id for r in found} <= node_ids
