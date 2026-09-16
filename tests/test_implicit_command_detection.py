"""
`/update` and `/updatemore` are DEFAULTS — the operator never types either prefix.

These cover the three questions that决定 whether that is safe:

  1. Does a real request reach the right Jenkins job?
  2. Does ordinary chat ever reach Jenkins?      (it must not)
  3. Does an INFERRED request ever fill a form without asking?  (it must not)

Every test drives the real `handle_lark_jenkins_update_message`. A fake `maintenance_mail` is
installed before the import so no SMTP/IMAP path can run, and the job dispatch is stubbed so no
Playwright browser is launched and no Jenkins form is touched.
"""
import json
import sys
import types

import pytest


def _install_fake_maintenance_mail():
    """Mail must be impossible, not merely unlikely — see tests/test_updatemore_queue_lifecycle.py."""
    fake = types.ModuleType("maintenance_mail")

    def _boom(*_a, **_k):
        raise AssertionError("maintenance_mail was reached from a test")

    fake.__getattr__ = lambda _name: _boom
    sys.modules["maintenance_mail"] = fake


_install_fake_maintenance_mail()

import jenkinsupdate as ju  # noqa: E402

CFG1 = "\nBranch: master\nVersion: 1.0.0\nServices: admin-rollout"
CFG2 = "\nBranch: dev\nVersion: 2.0.0\nServices: pms-api"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Stub the three things that otherwise reach the network — see the offline-repro notes."""
    monkeypatch.setattr(ju, "_fpms_lark_wrap_thread_send", lambda _c, _k, s: s)
    monkeypatch.setattr(ju, "_fpms_lark_begin_update_thread", lambda *_a, **_k: None)
    ju._fpms_lark_sessions.clear()
    yield
    ju._fpms_lark_sessions.clear()


class Run:
    """Outcome of one message: what was dispatched, what was said, what card was shown."""

    def __init__(self, dispatched, messages, cards):
        self.dispatched = dispatched
        self.messages = messages
        self.cards = cards

    @property
    def built(self):
        """True when a Jenkins form would have been filled without asking first."""
        return bool(self.dispatched)

    @property
    def picker(self):
        for card in self.cards:
            if card.get("header", {}).get("title", {}).get("content", "").startswith("Jenkins job"):
                return [
                    e["text"]["content"]
                    for e in card["body"]["elements"]
                    if e.get("tag") == "button"
                ]
        return None

    @property
    def segments(self):
        """Segment count when the message became an /updatemore, else 0."""
        import re

        for text in self.messages:
            m = re.search(r"\*\*/updatemore\*\* — (\d+) segment", text)
            if m:
                return int(m.group(1))
        return 0


def run(monkeypatch, body, *, implicit=True):
    dispatched, messages, cards = [], [], []
    monkeypatch.setattr(
        ju,
        "_fpms_lark_dispatch_job_row",
        lambda _c, _k, _b, row, _s, **_kw: (dispatched.append(row[2]) or True),
    )
    for name, label in [
        ("_fpms_lark_dispatch_bi_api_update_parameter_flow", "BI API UPDATE"),
        ("_fpms_lark_dispatch_fpms_prod_script_parameter_flow", "FPMS PROD SCRIPT"),
        ("_fpms_lark_dispatch_igo_prod_script_parameter_flow", "IGO PROD SCRIPT"),
        ("_fpms_lark_dispatch_cpms_igo_uat_parameter_flow", "CPMS/IGO UAT"),
    ]:
        monkeypatch.setattr(
            ju, name, (lambda l: lambda *_a, **_k: (dispatched.append(l) or True))(label)
        )

    def send(_chat_id, text, **kw):
        if kw.get("msg_type") == "interactive":
            cards.append(json.loads(text))
        else:
            messages.append(text)

    ju._fpms_lark_sessions.clear()
    ju.handle_lark_jenkins_update_message(
        "chat", "user", body, body, send, allow_start=True, implicit=implicit
    )
    return Run(dispatched, messages, cards)


# --------------------------------------------------------------------------- routing


def test_a_bi_api_request_is_not_mangled_into_a_script_picker(monkeypatch):
    """
    The agent normalizer only re-emits Branch/Version/Services, so it DELETED the ``API:`` line
    and promoted ``ENV: prod`` to the headline — and a bare ``prod`` fuzzy-matches all three
    ``* prod script`` aliases. The operator got a pick-one card of three script runners and then a
    "Command must start with node" parse error.
    """
    r = run(
        monkeypatch,
        "hello, can you please help update API below? thankieww\n"
        "\nAPI: ds-clickhouse-api\nENV: prod\nBranch: main",
    )
    assert r.dispatched == ["BI API UPDATE"]
    assert r.picker is None


def test_the_reported_igo_script_request_offers_the_right_job(monkeypatch):
    r = run(monkeypatch, "igo prod update\nnode addPlayerRank.js")
    assert r.picker is not None
    assert any("IGO PROD SCRIPT RUN" in b for b in r.picker)


def test_a_job_named_in_the_headline_survives_an_inferred_environment(monkeypatch):
    """``fpms uat fgs``: the banner invents ``fpms-uat-branch`` and used to delete FPMS FGS."""
    r = run(monkeypatch, "fpms uat fgs")
    assert r.picker is not None
    assert any("FPMS FGS" in b for b in r.picker)


# --------------------------------------------------------------------------- never build chatter


@pytest.mark.parametrize(
    "body",
    [
        "did the pms update finish?",
        "the pms build failed again",
        "ds is slow today",
        "nt master looks broken",
        "rc uat finished ok",
        "cpms uat failed",
        "please dont update anything now",
        "thanks!",
        "hi",
    ],
)
def test_ordinary_chat_never_fills_a_form(monkeypatch, body):
    assert not run(monkeypatch, body).built


def test_a_greeting_headline_with_a_config_block_asks_first(monkeypatch):
    """``strong`` needs a job NAME too: normalize makes line 1 the headline, greeting included."""
    r = run(monkeypatch, "hi team\nbrazil uat pms\nbranch: master\nservices: pms-api")
    assert not r.built


# --------------------------------------------------------------------------- /update default


def test_a_config_block_with_a_named_job_dispatches_without_an_extra_tap(monkeypatch):
    assert run(monkeypatch, "fpms uat master" + CFG1).dispatched == ["FPMS UAT MASTER UPDATE"]


def test_explicit_update_is_never_given_the_confirm_step(monkeypatch):
    assert run(monkeypatch, "/update fpms uat master" + CFG1).built


def test_implicit_off_preserves_the_old_behaviour(monkeypatch):
    assert not run(monkeypatch, "fpms uat fgs", implicit=False).built
    assert run(monkeypatch, "/update fpms uat master" + CFG1, implicit=False).built


# --------------------------------------------------------------------------- /updatemore default


@pytest.mark.parametrize(
    "name,body,want",
    [
        ("verb-first", "update fpms uat master" + CFG1 + "\nupdate pms uat" + CFG2, 2),
        ("noun-first", "fpms uat master" + CFG1 + "\npms uat" + CFG2, 2),
        ("mixed", "update fpms uat master" + CFG1 + "\nsms uat update" + CFG2, 2),
        ("three jobs", "update fpms uat master" + CFG1 + "\npms uat" + CFG2 + "\nsms uat" + CFG2, 3),
        ("two script jobs", "igo prod script\nnode a.js\nfpms prod script\nnode b.js", 2),
    ],
)
def test_a_multi_job_message_becomes_updatemore(monkeypatch, name, body, want):
    assert run(monkeypatch, body).segments == want


@pytest.mark.parametrize(
    "name,body",
    [
        ("single job", "update fpms uat master" + CFG1),
        ("single job noun-first", "fpms uat master" + CFG1),
        (
            "multi-line services",
            "update fpms uat master\nBranch: master\nVersion: 1.0\n"
            "Services:\nadmin-rollout\nrisk-rollout\npay-callback",
        ),
        ("ports as services", "update fpms uat master\nBranch: master\nVersion: 1.0\nServices:\n3000\n9000"),
        ("chatter naming two jobs", "is fpms uat master or pms uat down?"),
    ],
)
def test_a_single_job_message_never_becomes_updatemore(monkeypatch, name, body):
    assert run(monkeypatch, body).segments == 0


def test_explicit_updatemore_still_segments(monkeypatch):
    body = "/updatemore\nupdate fpms uat master" + CFG1 + "\nupdate pms uat" + CFG2
    assert run(monkeypatch, body).segments == 2
    assert run(monkeypatch, body, implicit=False).segments == 2


# --------------------------------------------------------------------------- headline vs data


@pytest.mark.parametrize(
    "line", ["fpms uat master", "pms uat", "update pms uat", "igo prod script", "sms uat update"]
)
def test_a_job_name_reads_as_a_headline(line):
    assert ju._line_is_job_headline(line)


@pytest.mark.parametrize(
    "line",
    [
        "pms-api",           # literal-matches alias `pms` at offset 0 and scores 12.003
        "rc-uat-service",    # literal-matches alias `rc uat`
        "admin-rollout",
        "livechat-rollout",
        "pay-callback",
        "node addPlayerRank.js",
        "hi team",
        "3000",
    ],
)
def test_a_service_id_never_reads_as_a_headline(line):
    assert not ju._line_is_job_headline(line)


# --------------------------------------------------------------------------- card display


def test_the_card_title_names_the_job_the_link_points_at():
    """A profile is a form shape shared by several jobs; only the URL identifies the job."""
    card = json.loads(
        ju._fpms_lark_verification_card_json(
            filled_env="igo-prod",
            filled_branch="node addPlayerRank.js",
            ok_all=True,
            build_url="https://jenkins.internal.client8.me/job/IGO/job/PROD/job/IGO-PROD-SCRIPT-RUN/build?delay=0sec",
            job_profile="fpms_prod_script",
            next_build_number=274,
        )
    )
    title = card["header"]["title"]["content"]
    assert title.startswith("IGO PROD SCRIPT RUN")
    assert "FPMS" not in title
    body = card["body"]["elements"][0]["text"]["content"]
    assert "**Command:** `node addPlayerRank.js`" in body
    assert "Branch:" not in body


def test_every_registered_job_gets_a_distinct_card_title():
    """The eight FRONTEND jobs share a leaf name and differ only in the ``uat-N`` folder."""
    seen = {}
    for _alias, (_label, raw) in ju.JENKINS_UPDATE_JOB_REGISTRY.items():
        for url in str(raw).splitlines():
            url = url.strip()
            if not url:
                continue
            name = ju._jenkins_job_display_name_for_url(url)
            assert name, url
            assert seen.setdefault(name, url) == url, f"{name!r} used by two jobs"


def test_the_job_picker_pairs_each_button_with_its_own_link():
    ties = ju._jenkins_update_disambiguation_ties(
        ju._rank_jenkins_update_job_matches("igo prod update"), band=0.08
    )
    elements = json.loads(ju._fpms_lark_job_choice_card_json(ties, picker_sid="x"))["body"]["elements"]
    tags = [e.get("tag") for e in elements]
    first = tags.index("button")
    # button, link, button, link, … so the URL always sits under the button that triggers it.
    assert tags[first : first + 4] == ["button", "div", "button", "div"]
    labels = [e["text"]["content"] for e in elements if e.get("tag") == "button"]
    assert not any(l.strip().isdigit() for l in labels), labels
