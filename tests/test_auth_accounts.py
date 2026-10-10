import json
import sqlite3
import stat
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from thermaestro.auth import (
    AccountError,
    Accounts,
    AddressLimiter,
    Forbidden,
    LoginFailed,
    Principal,
    SetupCode,
    passwords,
)
from thermaestro.auth.accounts import DISABLE_AFTER, SESSION_IDLE_S, SESSION_MAX_S
from thermaestro.cli import main
from thermaestro.core import AuditLog
from thermaestro.store import Database

GOOD = "correct horse battery staple"
FAST = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    async with await Database.open(tmp_path / "t.db") as db:
        yield db


@pytest.fixture
def accounts(db: Database, tmp_path: Path, clock: Clock) -> Accounts:
    return Accounts(db, AuditLog(tmp_path / "audit"), hasher=FAST, clock=clock)


def audit(tmp_path: Path) -> list[dict[str, object]]:
    lines = (tmp_path / "audit" / "audit.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


# --- passwords ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "password",
    [
        "short",
        "a" * 257,
        "films+pic+galeries",  # common, and long enough
        "!QAZXSW2#EDCVFR4",  # breached, any case
        "password1234567",  # a common one padded with digits
        "!!!monkey2024!!!",  # and with symbols
        "aaaaaaaaaaaaaaa",  # a repeat
        "121212121212121",
        "1234567890123456",  # a run
        "zyxwvutsrqponmlk",
        "my thermaestro password",
        "anna's long password",
    ],
)
def test_weak_passwords_are_refused(password: str) -> None:
    with pytest.raises(passwords.WeakPassword):
        passwords.check(password, user="anna")


@pytest.mark.parametrize(
    "password",
    [
        GOOD,
        "839274651029384",  # random digits are fine
        "the kettle sings at seven",
        "Tre små grisar i en sko",  # any language, any letters
    ],
)
def test_long_uncommon_passwords_are_fine(password: str) -> None:
    passwords.check(password, user="anna")


def test_the_blocklists_are_loaded() -> None:
    common, breached = passwords._common(), passwords._breached()
    assert len(common) == 10_001
    assert all(len(p) >= 4 for p in common)
    assert len(breached) > 10_000
    assert all(len(p) >= passwords.MIN_LENGTH for p in breached)
    assert all(p == p.lower() for p in breached)


async def test_hash_and_verify() -> None:
    stored = await passwords.hash_password(GOOD, FAST)
    assert stored.startswith("$argon2id$")
    assert await passwords.verify(stored, GOOD, FAST)
    assert not await passwords.verify(stored, GOOD + "!", FAST)
    assert not await passwords.verify("not a hash", GOOD, FAST)


async def test_unicode_forms_match() -> None:
    stored = await passwords.hash_password("ﬁlled with ångström units", FAST)
    assert await passwords.verify(stored, "filled with ångström units", FAST)


def test_the_real_parameters() -> None:
    assert passwords.HASHER.memory_cost == 19 * 1024
    assert passwords.HASHER.time_cost == 2
    assert passwords.HASHER.parallelism == 1


# --- users and groups --------------------------------------------------------------------


async def test_the_seeded_groups(accounts: Accounts) -> None:
    assert await accounts.groups() == {
        "Administrators": frozenset({"*"}),
        "Household": frozenset(
            {
                "points.read",
                "intent.temporary.create",
                "intent.temporary.create.away",
                "intent.temporary.create.guests",
                "plan.read",
            }
        ),
        "Viewers": frozenset({"points.read"}),
        "MQTT": frozenset(),  # requests over MQTT: nothing until the administrator says
    }


async def test_create_a_user_and_read_rights(accounts: Accounts, tmp_path: Path) -> None:
    assert not await accounts.has_admin()
    anna = await accounts.create_user("anna", GOOD, ["Household"], by="cli")
    assert anna.groups == ("Household",)
    assert anna.permissions == (await accounts.groups())["Household"]
    assert not await accounts.has_admin()
    await accounts.create_user("bo", GOOD + " two", ["Administrators"], by="cli")
    assert await accounts.has_admin()
    assert (await accounts.user("ANNA")) == anna  # names ignore case
    entry = audit(tmp_path)[0]
    assert (entry["who"], entry["what"]) == ("cli", "user.create")
    assert GOOD not in (tmp_path / "audit" / "audit.jsonl").read_text()


async def test_bad_users_are_refused(accounts: Accounts) -> None:
    await accounts.create_user("anna", GOOD, by="cli")
    with pytest.raises(AccountError, match="already"):
        await accounts.create_user("Anna", GOOD, by="cli")
    with pytest.raises(AccountError, match="no such group"):
        await accounts.create_user("bo", GOOD, ["Wizards"], by="cli")
    with pytest.raises(AccountError, match="user name"):
        await accounts.create_user("bo bo", GOOD, by="cli")
    with pytest.raises(AccountError, match="too easy"):
        await accounts.create_user("bo", "films+pic+galeries", by="cli")
    assert [u.name for u in await accounts.users()] == ["anna"]


async def test_the_last_admin_stays(accounts: Accounts) -> None:
    await accounts.create_user("anna", GOOD, ["Administrators"], by="cli")
    with pytest.raises(AccountError, match="no user who can manage"):
        await accounts.delete_user("anna", by="cli")
    with pytest.raises(AccountError, match="no user who can manage"):
        await accounts.set_groups("anna", ["Viewers"], by="cli")
    with pytest.raises(AccountError, match="no user who can manage"):
        await accounts.set_group("Administrators", ["points.read"], by="cli")
    with pytest.raises(AccountError, match="no user who can manage"):
        await accounts.delete_group("Administrators", by="cli")
    assert await accounts.has_admin()
    await accounts.create_user("bo", GOOD + " two", ["Administrators"], by="cli")
    await accounts.delete_user("anna", by="cli")
    assert [u.name for u in await accounts.users()] == ["bo"]


async def test_groups_can_be_made_and_changed(accounts: Accounts) -> None:
    await accounts.set_group("Guests", ["points.read", "audit.read"], by="cli")
    anna = await accounts.create_user("anna", GOOD, ["Guests"], by="cli")
    assert anna.permissions == {"points.read", "audit.read"}
    with pytest.raises(AccountError, match="no such right"):
        await accounts.set_group("Guests", ["points.write"], by="cli")
    await accounts.delete_group("Guests", by="cli")
    anna_now = await accounts.user("anna")
    assert anna_now is not None
    assert anna_now.permissions == frozenset()


# --- logging in --------------------------------------------------------------------------


async def test_login(accounts: Accounts, tmp_path: Path) -> None:
    await accounts.create_user("anna", GOOD, by="cli")
    user = await accounts.authenticate("anna", GOOD, source="192.0.2.5")
    assert user.name == "anna"
    assert audit(tmp_path)[-1]["what"] == "login"
    assert audit(tmp_path)[-1]["who"] == "user:anna"


async def test_an_unknown_user_and_a_wrong_password_look_alike(
    accounts: Accounts, tmp_path: Path
) -> None:
    await accounts.create_user("anna", GOOD, by="cli")
    with pytest.raises(LoginFailed) as unknown:
        await accounts.authenticate("nobody", GOOD, source="192.0.2.5")
    with pytest.raises(LoginFailed) as wrong:
        await accounts.authenticate("anna", "wrong", source="192.0.2.5")
    assert str(unknown.value) == str(wrong.value)
    reasons = [(e["outcome"], e["why"]) for e in audit(tmp_path)[1:]]
    assert reasons == [("failed", "unknown"), ("failed", "password")]


async def test_failures_delay_the_next_try(accounts: Accounts, clock: Clock) -> None:
    await accounts.create_user("anna", GOOD, by="cli")
    for delay in (1, 2):
        with pytest.raises(LoginFailed):
            await accounts.authenticate("anna", "wrong", source="192.0.2.5")
        clock.now += delay
    with pytest.raises(LoginFailed):
        await accounts.authenticate("anna", "wrong", source="192.0.2.5")
    # Three failures: 4 s from the last. Even the right password waits.
    with pytest.raises(LoginFailed):
        await accounts.authenticate("anna", GOOD, source="192.0.2.5")
    clock.now += 4
    await accounts.authenticate("anna", GOOD, source="192.0.2.5")
    # A success clears the count.
    with pytest.raises(LoginFailed):
        await accounts.authenticate("anna", "wrong", source="192.0.2.5")
    clock.now += 1
    await accounts.authenticate("anna", GOOD, source="192.0.2.5")


async def test_the_delay_is_capped_and_the_account_disabled(
    accounts: Accounts, db: Database, clock: Clock
) -> None:
    await accounts.create_user("anna", GOOD, by="cli")
    await accounts.create_user("bo", GOOD + " two", ["Administrators"], by="cli")
    await db.run(lambda t: t.execute("UPDATE users SET failures = 98 WHERE name = 'anna'"))
    with pytest.raises(LoginFailed):
        await accounts.authenticate("anna", "wrong", source="192.0.2.5")
    row = await db.run(
        lambda t: t.execute(
            "SELECT locked_until, disabled FROM users WHERE name = 'anna'"
        ).fetchone()
    )
    assert row == (clock.now + 900, 0)
    clock.now += 900
    with pytest.raises(LoginFailed):
        await accounts.authenticate("anna", "wrong", source="192.0.2.5")
    clock.now += 100_000
    with pytest.raises(LoginFailed):  # disabled after DISABLE_AFTER failures
        await accounts.authenticate("anna", GOOD, source="192.0.2.5")
    anna = await accounts.user("anna")
    assert anna is not None
    assert anna.disabled
    assert DISABLE_AFTER == 100
    await accounts.set_password("anna", GOOD + " anew", by="cli")
    await accounts.authenticate("anna", GOOD + " anew", source="192.0.2.5")


async def test_an_address_gets_limited_tries(db: Database, tmp_path: Path, clock: Clock) -> None:
    limiter = AddressLimiter(tries=3, window_s=60)
    accounts = Accounts(db, AuditLog(tmp_path), hasher=FAST, clock=clock, limiter=limiter)
    await accounts.create_user("anna", GOOD, by="cli")
    for _ in range(3):
        await accounts.authenticate("anna", GOOD, source="192.0.2.5")
    with pytest.raises(LoginFailed):
        await accounts.authenticate("anna", GOOD, source="192.0.2.5")
    await accounts.authenticate("anna", GOOD, source="192.0.2.6")
    clock.now += 61
    await accounts.authenticate("anna", GOOD, source="192.0.2.5")


async def test_old_hashes_are_upgraded_at_login(db: Database, tmp_path: Path) -> None:
    weak = Accounts(db, AuditLog(tmp_path), hasher=PasswordHasher(1, 8, 1))
    await weak.create_user("anna", GOOD, by="cli")
    stronger = PasswordHasher(2, 16, 1)
    await Accounts(db, AuditLog(tmp_path), hasher=stronger).authenticate(
        "anna", GOOD, source="192.0.2.5"
    )
    stored = await db.run(lambda t: t.execute("SELECT password FROM users").fetchone()[0])
    assert not stronger.check_needs_rehash(stored)


# --- sessions ----------------------------------------------------------------------------


async def test_sessions(accounts: Accounts, db: Database, clock: Clock) -> None:
    anna = await accounts.create_user("anna", GOOD, by="cli")
    raw = await accounts.start_session(anna, source="192.0.2.5", agent="Firefox")
    stored = await db.run(lambda t: t.execute("SELECT hash FROM sessions").fetchone()[0])
    assert raw not in stored  # only the hash is kept
    session = await accounts.session(raw)
    assert session is not None
    assert session.user == anna
    assert await accounts.session(raw + "x") is None
    await accounts.end_session(raw)
    assert await accounts.session(raw) is None


async def test_sessions_end_when_idle(accounts: Accounts, clock: Clock) -> None:
    anna = await accounts.create_user("anna", GOOD, by="cli")
    raw = await accounts.start_session(anna)
    clock.now += SESSION_IDLE_S - 1
    assert await accounts.session(raw) is not None  # and this use counts
    clock.now += SESSION_IDLE_S - 1
    assert await accounts.session(raw) is not None
    clock.now += SESSION_IDLE_S
    assert await accounts.session(raw) is None


async def test_sessions_end_after_a_week_however_used(accounts: Accounts, clock: Clock) -> None:
    anna = await accounts.create_user("anna", GOOD, by="cli")
    raw = await accounts.start_session(anna)
    while clock.now < 1_000_000 + SESSION_MAX_S - 3600:
        clock.now += 3600
        assert await accounts.session(raw) is not None
    clock.now += 3600
    assert await accounts.session(raw) is None


async def test_a_new_password_ends_sessions(accounts: Accounts) -> None:
    anna = await accounts.create_user("anna", GOOD, by="cli")
    kept, ended = await accounts.start_session(anna), await accounts.start_session(anna)
    await accounts.set_password("anna", GOOD + " anew", by="cli", end_sessions=False)
    assert await accounts.session(kept) is not None
    await accounts.set_password("anna", GOOD + " again", by="cli")
    assert await accounts.session(ended) is None
    assert await accounts.session(kept) is None


async def test_step_up(accounts: Accounts, clock: Clock) -> None:
    anna = await accounts.create_user("anna", GOOD, by="cli")
    raw = await accounts.start_session(anna)
    session = await accounts.session(raw)
    assert session is not None
    assert accounts.confirmed_recently(session)  # the login itself
    clock.now += 900
    session = await accounts.session(raw)
    assert session is not None
    assert not accounts.confirmed_recently(session)
    with pytest.raises(AccountError):
        await accounts.confirm(session, "wrong", source="192.0.2.5")
    await accounts.confirm(session, GOOD, source="192.0.2.5")
    session = await accounts.session(raw)
    assert session is not None
    assert accounts.confirmed_recently(session)


async def test_users_see_and_end_their_sessions(accounts: Accounts, clock: Clock) -> None:
    anna = await accounts.create_user("anna", GOOD, by="cli")
    bo = await accounts.create_user("bo", GOOD + " two", by="cli")
    admin = await accounts.create_user("cy", GOOD + " three", ["Administrators"], by="cli")
    first = await accounts.start_session(anna, source="192.0.2.5", agent="Firefox")
    clock.now += 1
    second = await accounts.start_session(anna, source="192.0.2.6", agent="Kitchen tablet")
    listed = await accounts.sessions(anna)
    assert [s.agent for s in listed] == ["Kitchen tablet", "Firefox"]
    assert first not in str(listed)
    with pytest.raises(Forbidden):
        await accounts.end_session_by_id(Principal(bo), "anna", listed[0].id)
    with pytest.raises(AccountError, match="no such session"):
        await accounts.end_session_by_id(Principal(anna), "anna", "0" * 16)
    await accounts.end_session_by_id(Principal(anna), "anna", listed[0].id)
    assert await accounts.session(second) is None
    await accounts.end_session_by_id(Principal(admin), "anna", listed[1].id)
    assert await accounts.session(first) is None
    assert await accounts.sessions(anna) == []


# --- tokens ------------------------------------------------------------------------------


async def test_tokens(accounts: Accounts, db: Database, clock: Clock, tmp_path: Path) -> None:
    await accounts.set_group("Readers", ["points.read", "tokens.own", "audit.read"], by="cli")
    anna = await accounts.create_user("anna", GOOD, ["Readers"], by="cli")
    raw = await accounts.create_token(Principal(anna), "grafana", ["points.read"], days=2)
    assert raw.startswith("thm_")
    assert len(raw) > 40
    stored = await db.run(lambda t: t.execute("SELECT hash FROM tokens").fetchone()[0])
    assert raw not in stored
    principal = await accounts.principal_for_token(raw)
    assert principal is not None
    assert principal.name == "token:anna"
    assert principal.allows("points.read")
    assert not principal.allows("audit.read")  # anna may; the token may not
    with pytest.raises(Forbidden):
        principal.require("audit.read")
    [info] = await accounts.tokens(anna)
    assert (info.name, info.permissions, info.last_used) == ("grafana", ("points.read",), clock.now)
    assert raw not in (tmp_path / "audit" / "audit.jsonl").read_text()
    clock.now += 2 * 86_400
    assert await accounts.principal_for_token(raw) is None


async def test_a_token_carries_at_most_its_users_rights(accounts: Accounts) -> None:
    await accounts.set_group("Readers", ["points.read", "tokens.own"], by="cli")
    anna = await accounts.create_user("anna", GOOD, ["Readers"], by="cli")
    me = Principal(anna)
    for wanted in (["users.manage"], ["*"], ["no.such"]):
        with pytest.raises(AccountError):
            await accounts.create_token(me, "t", wanted)
    viewer = await accounts.create_user("bo", GOOD + " two", ["Viewers"], by="cli")
    with pytest.raises(Forbidden):  # Viewers may not hold tokens
        await accounts.create_token(Principal(viewer), "t", ["points.read"])


async def test_a_token_loses_rights_its_user_loses(accounts: Accounts) -> None:
    await accounts.set_group("Readers", ["points.read", "tokens.own"], by="cli")
    anna = await accounts.create_user("anna", GOOD, ["Readers"], by="cli")
    raw = await accounts.create_token(Principal(anna), "t", ["points.read"])
    await accounts.set_groups("anna", [], by="cli")
    principal = await accounts.principal_for_token(raw)
    assert principal is not None
    assert not principal.allows("points.read")


async def test_revoking_tokens(accounts: Accounts) -> None:
    await accounts.set_group("Readers", ["points.read", "tokens.own"], by="cli")
    anna = await accounts.create_user("anna", GOOD, ["Readers"], by="cli")
    bo = await accounts.create_user("bo", GOOD + " two", ["Readers"], by="cli")
    admin = await accounts.create_user("cy", GOOD + " three", ["Administrators"], by="cli")
    raw = await accounts.create_token(Principal(anna), "t", ["points.read"])
    [info] = await accounts.tokens(anna)
    with pytest.raises(AccountError, match="no such token"):
        await accounts.revoke_token(Principal(bo), info.id)
    await accounts.revoke_token(Principal(admin), info.id)
    assert await accounts.principal_for_token(raw) is None


async def test_a_disabled_user_has_no_sessions_or_tokens(accounts: Accounts, db: Database) -> None:
    await accounts.set_group("Readers", ["points.read", "tokens.own"], by="cli")
    anna = await accounts.create_user("anna", GOOD, ["Readers"], by="cli")
    session = await accounts.start_session(anna)
    token = await accounts.create_token(Principal(anna), "t", ["points.read"])
    await db.run(lambda t: t.execute("UPDATE users SET disabled = 1"))
    assert await accounts.session(session) is None
    assert await accounts.principal_for_token(token) is None


async def test_a_page_refreshing_itself_doesnt_keep_the_session(
    accounts: Accounts, clock: Clock
) -> None:
    anna = await accounts.create_user("anna", GOOD, by="cli")
    raw = await accounts.start_session(anna)
    while clock.now < 1_000_000 + SESSION_IDLE_S - 120:
        clock.now += 60
        assert await accounts.session(raw, active=False) is not None
    clock.now += 120
    assert await accounts.session(raw, active=False) is None


# --- wall displays -----------------------------------------------------------------------


async def test_a_wall_display(
    accounts: Accounts, db: Database, clock: Clock, tmp_path: Path
) -> None:
    await accounts.set_group(
        "Readers", ["points.read", "plan.read", "audit.read", "wall_displays.own"], by="cli"
    )
    anna = await accounts.create_user("anna", GOOD, ["Readers"], by="cli")
    link = await accounts.create_display(Principal(anna), " Kitchen ", ["points.read", "plan.read"])
    assert link.startswith("thd_")
    assert await accounts.display_link(link) == ("Kitchen", "anna")
    [made] = await accounts.displays(anna)
    assert (made.name, made.permissions, made.opened) == (
        "Kitchen",
        ("plan.read", "points.read"),
        None,
    )
    assert made.link_expires == clock.now + 86_400
    raw = await accounts.open_display(link, source="192.0.2.9", agent="Tablet")
    # The link is used up; only its hash was ever kept.
    assert await accounts.display_link(link) is None
    with pytest.raises(AccountError, match="has been used"):
        await accounts.open_display(link)
    assert link not in (tmp_path / "audit" / "audit.jsonl").read_text()
    stored = await db.run(lambda t: [h for (h,) in t.execute("SELECT hash FROM sessions")])
    assert raw not in stored
    # It never idles out, and carries only its rights.
    for _ in range(3):
        clock.now += 30 * 86_400
        assert await accounts.session(raw, active=False) is not None
    session = await accounts.session(raw)
    assert session is not None
    principal = session.principal()
    assert principal.name == "display:anna"
    assert principal.allows("plan.read")
    assert not principal.allows("audit.read")  # anna may; the display may not
    with pytest.raises(AccountError, match="can't make changes that need a password"):
        await accounts.confirm(session, GOOD, source="192.0.2.9")
    [listed] = await accounts.sessions(anna)
    assert (listed.display, listed.agent) == ("Kitchen", "Tablet")
    [opened] = await accounts.displays(anna)
    assert (opened.opened, opened.link_expires) == (1_000_000.0, None)
    assert opened.last_seen == clock.now
    assert [e["what"] for e in audit(tmp_path)][-2:] == ["wall_display.create", "wall_display.open"]


async def test_a_display_ends_when_revoked_or_the_password_changes(
    accounts: Accounts, clock: Clock
) -> None:
    await accounts.set_group("Readers", ["points.read", "wall_displays.own"], by="cli")
    anna = await accounts.create_user("anna", GOOD, ["Readers"], by="cli")
    bo = await accounts.create_user("bo", GOOD + " two", ["Readers"], by="cli")
    first = await accounts.open_display(
        await accounts.create_display(Principal(anna), "hall", ["points.read"])
    )
    second = await accounts.open_display(
        await accounts.create_display(Principal(anna), "kitchen", ["points.read"])
    )
    hall, _ = await accounts.displays(anna)
    with pytest.raises(AccountError, match="no such display"):
        await accounts.revoke_display(Principal(bo), hall.id)
    await accounts.revoke_display(Principal(anna), hall.id)
    assert await accounts.session(first) is None
    assert [d.name for d in await accounts.displays(anna)] == ["kitchen"]
    await accounts.set_password("anna", GOOD + " anew", by="cli")
    assert await accounts.session(second) is None
    [kitchen] = await accounts.displays(anna)
    assert kitchen.last_seen is None  # listed as logged out, until revoked


async def test_a_display_link_lasts_a_day_and_carries_at_most_its_users_rights(
    accounts: Accounts, clock: Clock
) -> None:
    await accounts.set_group("Readers", ["points.read", "wall_displays.own"], by="cli")
    anna = await accounts.create_user("anna", GOOD, ["Readers"], by="cli")
    me = Principal(anna)
    for wanted in (["users.manage"], ["*"], ["no.such"]):
        with pytest.raises(AccountError):
            await accounts.create_display(me, "d", wanted)
    with pytest.raises(AccountError, match="1 to 64"):
        await accounts.create_display(me, "  ", ["points.read"])
    viewer = await accounts.create_user("bo", GOOD + " two", ["Viewers"], by="cli")
    with pytest.raises(Forbidden):
        await accounts.create_display(Principal(viewer), "d", ["points.read"])
    link = await accounts.create_display(me, "d", ["points.read"])
    clock.now += 86_400
    assert await accounts.display_link(link) is None
    with pytest.raises(AccountError, match="has expired"):
        await accounts.open_display(link)
    with pytest.raises(AccountError):
        await accounts.open_display("thm_" + link[4:])  # a token isn't a display link


# --- the first administrator -------------------------------------------------------------


def test_setup_codes(tmp_path: Path, clock: Clock) -> None:
    setup = SetupCode(tmp_path / "setup-code", clock)
    assert setup.current() is None
    code = setup.issue()
    assert stat.S_IMODE((tmp_path / "setup-code").stat().st_mode) == 0o600
    assert setup.current() == code
    assert not setup.redeem("WRONG-CODE-HERE")
    assert not setup.redeem("ÅÄÖ")
    assert setup.redeem(code.lower().replace("-", " "))  # forgiving to type
    assert not setup.redeem(code)  # once only
    code = setup.issue()
    clock.now += 3600
    assert setup.current() is None
    assert not setup.redeem(code)


async def test_the_first_admin_needs_the_code(
    accounts: Accounts, tmp_path: Path, clock: Clock
) -> None:
    setup = SetupCode(tmp_path / "setup-code", clock)
    code = setup.issue()
    with pytest.raises(AccountError, match="setup code"):
        await accounts.create_first_admin(setup, "XXXX-XXXX-XXXX", "anna", GOOD, source="x")
    with pytest.raises(AccountError, match="too easy"):  # and the code isn't used up
        await accounts.create_first_admin(setup, code, "anna", "anna1234567890xy", source="x")
    anna = await accounts.create_first_admin(setup, code, "anna", GOOD, source="x")
    assert anna.groups == ("Administrators",)
    assert setup.current() is None
    setup.issue()
    with pytest.raises(AccountError, match="already"):
        await accounts.create_first_admin(setup, setup.current() or "", "bo", GOOD, source="x")


# --- the command line --------------------------------------------------------------------


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "thermaestro.toml").write_text("")
    monkeypatch.setenv("CONFIGURATION_DIRECTORY", str(tmp_path / "config"))
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "state"))
    return tmp_path / "state"


def answers(monkeypatch: pytest.MonkeyPatch, *given: str) -> None:
    replies = iter(given)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(replies))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(replies))


def test_cli_setup_code(
    host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["setup-code"]) == 0
    code = capsys.readouterr().out.strip()
    assert main(["setup-code"]) == 0
    assert capsys.readouterr().out.strip() == code  # the same, while it lasts
    answers(monkeypatch, GOOD, GOOD)
    assert main(["admin", "create", "anna"]) == 0
    assert main(["setup-code"]) == 1


def test_cli_admin(
    host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers(monkeypatch, GOOD, GOOD + "x")
    assert main(["admin", "create", "anna"]) == 1
    assert "differ" in capsys.readouterr().err
    answers(monkeypatch, "short", "short")
    assert main(["admin", "create", "anna"]) == 1
    answers(monkeypatch, GOOD, GOOD)
    assert main(["admin", "create", "anna", "--group", "Household"]) == 0
    answers(monkeypatch, GOOD, GOOD)
    assert main(["admin", "reset-password", "nobody"]) == 1
    answers(monkeypatch, GOOD + " anew", GOOD + " anew", "n")
    assert main(["admin", "reset-password", "anna"]) == 0
    with closing(sqlite3.connect(host / "thermaestro.db")) as db:
        assert db.execute("SELECT name FROM users").fetchall() == [("anna",)]
    entries = [
        json.loads(line) for line in (host / "audit" / "audit.jsonl").read_text().splitlines()
    ]
    assert [(e["who"], e["what"]) for e in entries] == [
        ("cli", "user.create"),
        ("cli", "user.password"),
    ]
    assert entries[1]["details"] == {"user": "anna", "sessions_ended": False}


def test_the_address_limiter_forgets_old_addresses() -> None:
    limiter = AddressLimiter(tries=1, window_s=60)
    for n in range(10_001):
        assert limiter.allow(f"192.0.2.{n}", now=0)
    assert limiter.allow("198.51.100.1", now=1000)
    assert len(limiter._seen) == 1
