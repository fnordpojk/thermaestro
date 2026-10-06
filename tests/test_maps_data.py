"""The tables that ship: what's known about the models Thermaestro supports first."""

import pytest

from thermaestro.nibe.maps import RegisterMap, Size, Status, UnknownSize, decode, load, words


@pytest.fixture(scope="module")
def bus() -> RegisterMap:
    return load("bus")


@pytest.fixture(scope="module")
def s_series() -> RegisterMap:
    return load("s-series")


def test_the_f1245(bus: RegisterMap) -> None:
    f1245 = bus.model("F1245")
    bt1 = f1245.register(40004)
    assert (bt1.size, bt1.factor, bt1.writable, bt1.known) == (Size.S16, 10, False, "documented")
    offset = f1245.register(47011)
    assert (offset.size, offset.writable, offset.min, offset.max) == (Size.S8, True, -10, 10)
    swap = f1245.register(48852)
    assert (swap.size, swap.writable, swap.default) == (Size.U8, True, 1)
    # Compressor starts, as a pump answered with 48852 = 0.
    starts = decode(f1245.register(43416), *words(bytes.fromhex("0100f355")), high_word_first=True)
    assert (starts.value, starts.status) == (87_539, Status.OK)


def test_registers_only_nibepi_lists_are_reported(bus: RegisterMap) -> None:
    for register in (43110, 43111, 47208, 47260):
        r = bus.model("F1145").register(register)
        assert r.known == "reported"
        assert "nibepi" in r.sources


def test_product_names_from_nibepi(bus: RegisterMap) -> None:
    assert bus.model("VPK8R").ids == bus.model("F1145").ids
    # NibePi's HMA60 file is an older export than Nibe's SMO 40 one, which has gained six
    # registers since.
    assert bus.model("SMO40").ids - bus.model("HMA60").ids == set(range(48659, 48665))
    assert bus.model("HMA60").ids <= bus.model("SMO40").ids
    assert not any(name.startswith("RMU") for name in bus.models)


def test_the_s_series(s_series: RegisterMap) -> None:
    s1255 = s_series.model("S1255")
    bt1 = s1255.register(30001)
    assert (bt1.factor, bt1.writable, bt1.official_name) == (10, False, "eS16BT1Outdoor_0")
    limit = s1255.register(40018)
    assert limit.factor == 10
    assert ("nibepi", "factor", "1") in [(d.source, d.field, d.value) for d in limit.disagreements]
    with pytest.raises(UnknownSize):
        decode(s1255.register(40067), 0, 0, high_word_first=True)
    assert {"VVMS320", "SMOS40", "S735"} <= set(s_series.models)


def test_every_listed_register_resolves(bus: RegisterMap, s_series: RegisterMap) -> None:
    for table in (bus, s_series):
        for model in table.models.values():
            for register in model:
                assert model.register(register).id == register
