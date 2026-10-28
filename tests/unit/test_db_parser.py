import textwrap

from ripple.graph.schema import DBFacts
from ripple.parsers.db_parser import (
    parse_db_file,
    parse_db_source,
    parse_models_source,
)
from ripple.parsers.python_parser import parse_python_source

PATH = "app/models.py"


def src_of(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def models(text: str, path: str = PATH):
    return parse_models_source(src_of(text), path)


def rows(text: str):
    return [
        (m.class_id.partition("::")[2], m.table, m.abstract, m.non_literal)
        for m in models(text)
    ]


def test_literal_tablename_plain_and_annotated():
    assert rows("""
        class User(Base):
            __tablename__ = "users"

        class Order(Base):
            __tablename__: str = "orders"
        """) == [
        ("User", "users", False, False),
        ("Order", "orders", False, False),
    ]


def test_decl_fields():
    (m,) = models("""
        class User(Base):
            id = Column(Integer)
            __tablename__ = "users"
        """)
    assert (m.class_id, m.file_path, m.table, m.line) == (
        f"{PATH}::User",
        PATH,
        "users",
        3,
    )


def test_abstract_marker():
    assert rows("""
        class Base(DeclarativeBase):
            __abstract__ = True

        class Timestamped(Base):
            __abstract__ = True
            created = Column(DateTime)
        """) == [
        ("Base", None, True, False),
        ("Timestamped", None, True, False),
    ]


def test_abstract_line_is_used_when_there_is_no_tablename():
    (m,) = models("""
        class Base(DeclarativeBase):
            x = 1
            __abstract__ = True
        """)
    assert m.line == 3


def test_table_and_abstract_are_both_kept():
    assert rows("""
        class X(Base):
            __abstract__ = True
            __tablename__ = "x"
        """) == [("X", "x", True, False)]


def test_classes_without_markers_are_ignored():
    assert (
        rows("""
        class Plain:
            x = 1

        class NotAbstract:
            __abstract__ = False

        class Sub(Base):
            name = Column(String)
        """)
        == []
    )


def test_non_literal_tablename():
    assert rows("""
        PREFIX = "x_"

        class A(Base):
            __tablename__ = PREFIX + "a"

        class B(Base):
            __tablename__ = f"{PREFIX}b"

        class C(Base):
            @declared_attr
            def __tablename__(cls):
                return cls.__name__.lower()
        """) == [
        ("A", None, False, True),
        ("B", None, False, True),
        ("C", None, False, True),
    ]


def test_last_tablename_assignment_wins():
    assert rows("""
        class A(Base):
            __tablename__ = "old"
            __tablename__ = "new"
        """) == [("A", "new", False, False)]


def test_only_direct_class_body_statements_count():
    assert (
        rows("""
        class A(Base):
            def m(self):
                __tablename__ = "x"

            if DEBUG:
                __tablename__ = "y"
        """)
        == []
    )


def test_nested_classes_use_dotted_qualnames():
    found = models("""
        def factory():
            class Local(Base):
                __tablename__ = "local"
            return Local

        class Outer:
            class Inner(Base):
                __tablename__ = "inner"
        """)
    assert [m.class_id.partition("::")[2] for m in found] == [
        "factory.Local",
        "Outer.Inner",
    ]


def test_duplicate_class_first_wins():
    found = models("""
        class A(Base):
            __tablename__ = "first"

        class A(Base):
            __tablename__ = "second"
        """)
    assert [m.table for m in found] == ["first"]


def test_windows_path_is_normalized():
    (m,) = parse_models_source('class A(Base):\n    __tablename__ = "a"\n', "app\\m.py")
    assert m.file_path == "app/m.py" and m.class_id == "app/m.py::A"


def test_syntax_error_yields_nothing():
    assert models("class A(Base):\n    __tablename__ = 'x'\ndef broken(:\n") == []


def test_bundle_matches_parse_models_source():
    text = src_of("""
        class A(Base):
            __tablename__ = "a"
        """)
    assert parse_db_source(text, PATH).models == parse_models_source(text, PATH)


def test_bundle_syntax_error_is_empty_facts():
    assert parse_db_source("def broken(:\n", PATH) == DBFacts(path=PATH)


def test_parse_db_file_handles_non_utf8(tmp_path):
    p = tmp_path / "models.py"
    p.write_bytes(b'# caf\xe9\nclass A(Base):\n    __tablename__ = "a"\n')
    facts = parse_db_file(p, "models.py")
    assert [m.table for m in facts.models] == ["a"]


def test_class_ids_match_python_parser_nodes():
    text = src_of("""
        class A(Base):
            __tablename__ = "a"

        def factory():
            class B(Base):
                __tablename__ = "b"

        class Outer:
            class C(Base):
                __abstract__ = True
        """)
    node_ids = {n.id for n in parse_python_source(text, PATH).nodes}
    found = parse_models_source(text, PATH)
    assert len(found) == 3
    assert {m.class_id for m in found} <= node_ids
