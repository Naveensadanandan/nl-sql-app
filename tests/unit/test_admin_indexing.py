import pytest

from nlsql.worker.indexing import CsvError, GlossaryRow, parse_glossary_csv, point_id

GOOD = (
    "name,description,section,synonyms,user_id\n"
    "best customers,Top 5 by sales,business_definition,top customers|highest buyers,\n"
    "revenue,Sum of order totals,metric,,u_7\n"
)


def test_parse_ok() -> None:
    rows = parse_glossary_csv(GOOD.encode())
    assert rows[0].synonyms == ["top customers", "highest buyers"]
    assert rows[0].user_id is None
    assert rows[0].status == "approved"
    assert rows[1].user_id == "u_7"


def test_bom_is_accepted() -> None:
    assert parse_glossary_csv(b"\xef\xbb\xbf" + GOOD.encode())


@pytest.mark.parametrize(
    "raw",
    [
        b"name,description\nx,y\n",  # missing section column
        b"name,description,section\nx,y,bogus\n",  # bad section
        b"name,description,section\n",  # no rows
        b"\xff\xfe",  # not utf-8
    ],
)
def test_parse_rejects(raw: bytes) -> None:
    with pytest.raises(CsvError):
        parse_glossary_csv(raw)


def test_point_id_is_stable_and_tenant_scoped() -> None:
    row = GlossaryRow(name="Revenue", description="d", section="metric")
    assert point_id("acme", row) == point_id("acme", row)
    assert point_id("acme", row) != point_id("other", row)
