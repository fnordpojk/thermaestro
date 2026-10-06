import json

import pytest

from thermaestro.nibe.maps import RegisterMap, Size, from_json
from thermaestro.nibe.maps.build import (
    BuildError,
    build,
    read_extensions,
    read_modbusmanager,
    read_nibepi,
    read_official_list,
    read_s_export,
)

MM_HEADER = "ModbusManager 1.0.9\n20260927\nProduct: {products}\nDatabase: 9696\n"
MM_COLUMNS = "Title;Info;ID;Unit;Size;Factor;Min;Max;Default;Mode\n"


def mm(products: str, *rows: str) -> bytes:
    text = MM_HEADER.format(products=products) + MM_COLUMNS + "".join(r + "\n" for r in rows)
    return text.encode("latin-1")


BT1 = '"BT1 Outdoor Temperature";"Current outdoor temperature";40004;"°C";s16;10;0;0;0;R;'
OFFSET = '"Heat Offset S1";"";47011;"";s8;1;-10;10;0;R/W;'
MODE = '"Hot water comfort mode";"0=Economy 1=Normal 2=Luxury";47041;"";s8;1;0;4;1;R/W;'
STARTS = '"Compressor starts EB100-EP14";"";43416;"";s32;1;0;0;0;R;'


def table(**kw: object) -> RegisterMap:
    """Build, go through JSON as the shipped files do, and load."""
    space = str(kw.pop("space", "bus"))
    data = build(space, **kw)  # type: ignore[arg-type]
    return from_json(json.loads(json.dumps(data)))


def test_shared_registers_are_one_definition_with_per_model_membership() -> None:
    m = table(
        modbusmanager=[
            read_modbusmanager(mm("F1145, F1245", BT1, OFFSET, STARTS), "f1145-1245"),
            read_modbusmanager(mm("F370, F470", BT1, MODE), "f370-470"),
        ]
    )
    assert sorted(m.models) == ["F1145", "F1245", "F370", "F470"]
    bt1 = m.model("F1245").register(40004)
    assert (bt1.title, bt1.unit, bt1.size, bt1.factor) == (
        "BT1 Outdoor Temperature",
        "°C",
        Size.S16,
        10,
    )
    assert (bt1.min, bt1.max, bt1.writable, bt1.known) == (None, None, False, "documented")
    assert bt1.sources == ("nibe-db-9696",)
    assert m.model("F1245").register(47011).min == -10
    assert 47041 in m.model("F370")
    assert 47041 not in m.model("F1245")
    with pytest.raises(KeyError):
        m.model("F1245").register(47041)
    with pytest.raises(KeyError):
        m.model("F9999")


def test_a_conflict_inside_one_export_fails() -> None:
    clash = OFFSET.replace(";s8;", ";u8;")
    with pytest.raises(BuildError, match="47011"):
        read_modbusmanager(mm("F1245", OFFSET, clash), "f1245")


def test_models_that_disagree_get_variants() -> None:
    m = table(
        modbusmanager=[
            read_modbusmanager(mm("F1145", OFFSET), "a"),
            read_modbusmanager(mm("F1245", OFFSET), "b"),
            read_modbusmanager(mm("F1345", OFFSET.replace(";s8;", ";s16;")), "c"),
        ]
    )
    assert m.model("F1245").register(47011).size is Size.S8
    assert m.model("F1345").register(47011).size is Size.S16


NIBEPI = [
    {
        "register": "40004",
        "titel": "BT1 Utomhus",
        "info": "",
        "unit": "°C",
        "size": "s16",
        "factor": "10",
        "mode": "R",
        "min": "0",
        "max": "0",
    },
    {
        "register": "47260",
        "titel": "Fan Mode",
        "info": "",
        "unit": "",
        "size": "u8",
        "factor": "1",
        "mode": "R/W",
        "min": "0",
        "max": "4",
    },
]


def test_nibepi_adds_registers_and_models_as_reported() -> None:
    export = read_modbusmanager(mm("F1245", BT1), "f1245")
    m = table(
        modbusmanager=[export],
        nibepi=[
            read_nibepi(json.dumps(NIBEPI).encode(), "F1245"),
            read_nibepi(json.dumps(NIBEPI).encode(), "VPK8R"),
        ],
    )
    fan = m.model("F1245").register(47260)
    assert (fan.sources, fan.known, fan.max, fan.writable) == (("nibepi",), "reported", 4, True)
    assert m.model("F1245").register(40004).sources == ("nibe-db-9696", "nibepi")
    assert m.model("F1245").register(40004).known == "documented"
    assert m.model("VPK8R").ids == frozenset({40004, 47260})


def test_nibepi_never_overrides_a_documented_definition() -> None:
    other = [dict(NIBEPI[0], factor="1")]
    m = table(
        modbusmanager=[read_modbusmanager(mm("F1245", BT1), "f1245")],
        nibepi=[read_nibepi(json.dumps(other).encode(), "F1245")],
    )
    bt1 = m.model("F1245").register(40004)
    assert bt1.factor == 10
    assert [(d.source, d.field, d.value) for d in bt1.disagreements] == [("nibepi", "factor", "1")]


EXTENSIONS = [
    {
        "files": ["f1145_f1245.json"],
        "data": {
            "47041": {"mappings": {"1": "Normal", "2": "Luxury", "10": None}},
            "40004": {"factor": 1},
            "48852": {
                "title": "Modbus40 Word Swap",
                "info": "",
                "size": "u8",
                "factor": 1,
                "min": 0.0,
                "max": 1.0,
                "default": 1.0,
                "write": True,
            },
        },
    },
    {"files": [], "data": {"40004": {"mappings": {"0": "x"}}}},
]


def test_library_corrections_are_reported_and_never_override() -> None:
    m = table(
        modbusmanager=[read_modbusmanager(mm("F1145, F1245", BT1, MODE), "f1145-1245")],
        extensions=read_extensions(json.dumps(EXTENSIONS).encode()),
    )
    mode = m.model("F1245").register(47041)
    assert mode.mappings == {1: "Normal", 2: "Luxury"}
    assert "nibe-lib-ext" in mode.sources
    assert mode.known == "documented"
    bt1 = m.model("F1245").register(40004)
    assert bt1.factor == 10
    assert [(d.source, d.field) for d in bt1.disagreements] == [("nibe-lib-ext", "factor")]
    swap = m.model("F1145").register(48852)
    assert (swap.size, swap.default, swap.known, swap.writable) == (Size.U8, 1, "reported", True)


S_EXPORT = (
    "Title\tRegister type\tRegister\tDivision factor\tUnit\tSize of variable\t"
    "Min value\tMax value\tDefault value\n"
    "Current outdoor temperature (BT1)\tMODBUS_INPUT_REGISTER\t1\t10\t°C\ts16\t0\t0\t0\n"
    "Periodic hot water\tMODBUS_HOLDING_REGISTER\t65\t1\t\ts8\t0\t1\t1\n"
    "Start time periodic hot water\tMODBUS_HOLDING_REGISTER\t67\t1\t\t-\t0\t0\t0\n"
)


def test_s_series_numbering_writability_and_unknown_sizes() -> None:
    m = table(
        space="s-series",
        s_exports=[read_s_export(S_EXPORT.encode(), "s1155_s1255")],
        official=read_official_list(
            "eMbInput_eS16BT1Outdoor_0 = 1,\neMbHolding_eTimeXHwStart = 67,\n"
        ),
    )
    assert sorted(m.models) == ["S1155", "S1255"]
    s = m.model("S1255")
    assert (s.register(30001).writable, s.register(30001).factor) == (False, 10)
    assert s.register(40065).writable
    assert s.register(40067).size is None
    assert s.register(30001).official_name == "eS16BT1Outdoor_0"


def test_s_export_duplicates_prefer_the_named_entry() -> None:
    # Pump exports list some internal parameters as "id:NNNN", sometimes on a number that
    # also has a named register.
    rows = (
        "Temperature, Overload, pool\tMODBUS_HOLDING_REGISTER\t0\t10\t°C\tu8\t1\t50\t1\n"
        "id:25407\tMODBUS_HOLDING_REGISTER\t0\t1\t\tu8\t0\t0\t0\n"
        "id:8065\tMODBUS_HOLDING_REGISTER\t3264\t1\t\t-\t0\t0\t0\n"
        "id:12654\tMODBUS_HOLDING_REGISTER\t3264\t1\t\ts8\t0\t0\t0\n"
    )
    m = table(space="s-series", s_exports=[read_s_export((S_EXPORT + rows).encode(), "s735")])
    pool = m.model("S735").register(40000)
    assert (pool.title, pool.factor, pool.disagreements) == ("Temperature, Overload, pool", 10, ())
    clash = m.model("S735").register(43264)
    assert clash.size is Size.S8
    assert [(d.source, d.field, d.value) for d in clash.disagreements] == [
        ("nibe-lib", "size", "None")
    ]


def test_s_series_cross_check_with_nibepi() -> None:
    nibepi = [
        {
            "register": "30001",
            "titel": "BT1",
            "info": "",
            "unit": "°C",
            "size": "s16",
            "factor": "1",
            "mode": "R",
            "min": "0",
            "max": "0",
        },
        {
            "register": "31975",
            "titel": "Larmnummer",
            "info": "",
            "unit": "",
            "size": "u16",
            "factor": "1",
            "mode": "R",
            "min": "0",
            "max": "0",
        },
    ]
    m = table(
        space="s-series",
        s_exports=[read_s_export(S_EXPORT.encode(), "s1155_s1255")],
        nibepi=[read_nibepi(json.dumps(nibepi).encode(), "S1255")],
    )
    bt1 = m.model("S1255").register(30001)
    assert bt1.factor == 10
    assert [(d.source, d.field, d.value) for d in bt1.disagreements] == [("nibepi", "factor", "1")]
    alarm = m.model("S1255").register(31975)
    assert (alarm.sources, alarm.known) == (("nibepi",), "reported")
    assert 31975 not in m.model("S1155")


def test_nibepi_duplicates_must_agree_on_type() -> None:
    twice = [NIBEPI[1], dict(NIBEPI[1], titel="other title")]
    assert read_nibepi(json.dumps(twice).encode(), "X")[47260].title == "Fan Mode"
    with pytest.raises(BuildError, match="47260"):
        read_nibepi(json.dumps([NIBEPI[1], dict(NIBEPI[1], size="s16")]).encode(), "X")
