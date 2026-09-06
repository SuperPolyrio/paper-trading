from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_account_manager_static_entrypoint_is_complete_and_paper_only() -> None:
    html = (ROOT / "webpage/paper.html").read_text(encoding="utf-8")
    script = (ROOT / "webpage/paper-account.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "webpage/paper-account.css").read_text(encoding="utf-8")

    for required in (
        'data-view="orders"',
        'data-view="positions"',
        'data-view="fills"',
        'data-view="ledger"',
        'data-view="journal"',
        'data-view="performance"',
        'data-view="admin"',
        'id="orderAuditDialog"',
        'id="adminView"',
        'id="adminDlqTable"',
        'id="adminRetentionTable"',
        'id="adminBundlesTable"',
        'id="replayDataQuality"',
        'id="replayAttributionTable"',
        'id="researchView"',
        'id="scenarioTable"',
        'id="conditionalOrderTable"',
        'id="modelDisclosureDialog"',
        'id="modelDisclosureButton"',
        'id="exportResource"',
        'id="navChart"',
    ):
        assert required in html
    assert 'data-api-base="/v1/paper"' in html
    assert 'id="apiBaseInput" type="text" inputmode="url"' in html
    assert 'id="apiBaseInput" type="url"' not in html
    assert "sessionStorage.setItem(SESSION_KEY" in script
    assert "localStorage" not in script
    assert "Authorization: `Bearer ${state.apiKey}`" in script
    assert "/accounts/${state.accountId}/performance" in script
    assert "/audit/${orderId}" in script
    assert '"/admin/dashboard"' in script
    assert "/admin/evidence-bundles" in script
    assert "real order" not in (html + script).lower()
    assert "linear-gradient" not in stylesheet
    assert "radial-gradient" not in stylesheet


def test_retail_and_professional_workspaces_link_to_each_other() -> None:
    retail = (ROOT / "webpage/paper-retail.html").read_text(encoding="utf-8")
    professional = (ROOT / "webpage/paper.html").read_text(encoding="utf-8")

    assert 'href="/paper.html"' in retail
    assert 'href="/paper-retail.html"' in professional
