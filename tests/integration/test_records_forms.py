"""Every rendered POST form works as a browser would submit it (Verifier D-1 regression).

Forms are parsed from the HTML and submitted with exactly their own fields — including the
hidden `csrf_token` they carry — never with a token taken from the page's meta tag.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import TEST_SECRET
from tests.integration.test_records_repo import SAMPLES
from tests.integration.test_records_web import PEER, TAB_OF, A, T

ALL = sorted(TAB_OF)


@dataclass
class Form:
    method: str
    action: str
    fields: dict[str, str] = field(default_factory=dict)
    checkboxes: dict[str, str] = field(default_factory=dict)  # unchecked boxes: name → value


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self._form: Form | None = None
        self._select: str | None = None
        self._first_option: str | None = None
        self._selected = False
        self._textarea: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self._form = Form(a.get("method", "get").lower(), a.get("action", ""))
            self.forms.append(self._form)
        elif self._form is None:
            return
        elif tag == "input" and "name" in a:
            if a.get("type") == "checkbox":
                if "checked" in a:
                    self._form.fields[a["name"]] = a.get("value", "on")
                else:
                    self._form.checkboxes[a["name"]] = a.get("value", "on")
            else:
                self._form.fields[a["name"]] = a.get("value", "")
        elif tag == "select":
            self._select, self._first_option, self._selected = a["name"], None, False
        elif tag == "option" and self._select is not None:
            if self._first_option is None:
                self._first_option = a.get("value", "")
            if "selected" in a:
                self._form.fields[self._select], self._selected = a.get("value", ""), True
        elif tag == "textarea":
            self._textarea = a["name"]
            self._form.fields[a["name"]] = ""

    def handle_data(self, data: str) -> None:
        if self._form is not None and self._textarea is not None:
            self._form.fields[self._textarea] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self._form is not None and self._select is not None:
            if not self._selected:
                self._form.fields[self._select] = self._first_option or ""
            self._select = None
        elif tag == "textarea":
            self._textarea = None
        elif tag == "form":
            self._form = None


def forms(html: str) -> list[Form]:
    parser = _FormParser()
    parser.feed(html)
    return [f for f in parser.forms if f.method == "post"]


def one(html: str, action: str) -> Form:
    matches = [f for f in forms(html) if f.action == action]
    assert len(matches) == 1, (action, [f.action for f in forms(html)])
    return matches[0]


def submit(c: TestClient, form: Form, overrides: dict[str, Any] | None = None, *, check: bool = False) -> Any:
    data = dict(form.fields)
    if check:
        data.update(form.checkboxes)
    for k, v in (overrides or {}).items():
        assert k in data, f"{k} is not a field of {form.action}"
        data[k] = str(v)
    return c.post(form.action, data=data, follow_redirects=False)


def assert_tokens_present(html: str) -> None:
    assert 'name="csrf_token" value=""' not in html
    for f in forms(html):
        assert f.fields.get("csrf_token"), f"{f.action} has no CSRF token"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    settings = Settings.model_validate({"session_secret": TEST_SECRET, "database_path": tmp_path / "forms.db",
                                        "test_auth": True, "allowed_emails": A})
    return create_app(settings, today=lambda: T)


@pytest.fixture
def browser(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url="http://localhost", client=PEER) as c:
        page = c.get("/").text
        assert_tokens_present(page)
        r = submit(c, one(page, "/auth/test-login"), {"email": A})
        assert r.status_code == 303
        yield c


def test_no_page_renders_an_empty_csrf_token(browser: TestClient) -> None:
    for domain in ALL:
        submit(browser, one(browser.get(f"/{TAB_OF[domain]}").text, f"/records/{domain}"), SAMPLES[domain][0])
    paths = ["/", "/health", "/content", "/jobs", "/tasks", "/analyst", "/deleted",
             "/records/meals/1/edit", "/records/meals/1/delete"]
    for path in paths:
        page = browser.get(path)
        assert page.status_code == 200, path
        assert_tokens_present(page.text)


@pytest.mark.parametrize("domain", ALL)
def test_record_lifecycle_through_rendered_forms(browser: TestClient, domain: str) -> None:
    tab = f"/{TAB_OF[domain]}"
    # add (the tab's own add form, filled in like a user would)
    r = submit(browser, one(browser.get(tab).text, f"/records/{domain}"), SAMPLES[domain][0])
    assert r.status_code == 303, r.text
    page = browser.get(r.headers["location"]).text
    edit_link = re.search(rf'href="/records/{domain}/(\d+)/edit"', page)
    assert edit_link
    rid = edit_link.group(1)
    # edit: the prefilled form submitted unchanged, then with a change
    edit_page = browser.get(f"/records/{domain}/{rid}/edit").text
    assert_tokens_present(edit_page)
    edit_form = one(edit_page, f"/records/{domain}/{rid}/edit")
    assert submit(browser, edit_form).status_code == 303
    changes = {k: v for k, v in SAMPLES[domain][2].items() if k in edit_form.fields}
    assert submit(browser, edit_form, changes).status_code == 303
    # delete via the confirm page's form
    confirm = browser.get(f"/records/{domain}/{rid}/delete").text
    assert submit(browser, one(confirm, f"/records/{domain}/{rid}/delete")).status_code == 303
    # re-add the (edited) record's key: the conflict page's restore form works
    conflict = submit(browser, one(browser.get(tab).text, f"/records/{domain}"), SAMPLES[domain][2])
    assert conflict.status_code == 409
    assert submit(browser, one(conflict.text, f"/records/{domain}/{rid}/restore")).status_code == 303
    # delete again, restore from Recently deleted, delete again, purge with the box ticked
    confirm = browser.get(f"/records/{domain}/{rid}/delete").text
    assert submit(browser, one(confirm, f"/records/{domain}/{rid}/delete")).status_code == 303
    deleted = browser.get("/deleted").text
    assert_tokens_present(deleted)
    assert submit(browser, one(deleted, f"/records/{domain}/{rid}/restore")).status_code == 303
    confirm = browser.get(f"/records/{domain}/{rid}/delete").text
    assert submit(browser, one(confirm, f"/records/{domain}/{rid}/delete")).status_code == 303
    purge_page = browser.get(f"/records/{domain}/{rid}/purge").text
    assert_tokens_present(purge_page)
    purge = one(purge_page, f"/records/{domain}/{rid}/purge")
    assert submit(browser, purge).status_code == 400  # box not ticked
    r = submit(browser, purge, check=True)
    assert r.status_code == 303 and r.headers["location"] == "/deleted"
    assert browser.get(f"/records/{domain}/{rid}/edit").status_code == 404


def test_task_status_form_and_sign_out(browser: TestClient) -> None:
    add = one(browser.get("/tasks").text, "/records/tasks")
    assert submit(browser, add, {"task_date": "2026-10-19", "title": "Toggle me", "category": "other"}
                  ).status_code == 303
    page = browser.get("/tasks").text
    status = next(f for f in forms(page) if re.fullmatch(r"/records/tasks/\d+/status", f.action))
    assert status.fields["status"] == "completed"
    assert submit(browser, status).status_code == 303
    assert "Mark pending" in browser.get("/tasks").text
    r = submit(browser, one(browser.get("/tasks").text, "/logout"))
    assert r.status_code == 303 and "Test sign-in" in browser.get("/").text
