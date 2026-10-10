"""Bounded synchronous assertion swallowing in JS/TS test callbacks (#363).

Expectations are new label proposals pending maintainer review. Execute only
through remote CI; the work machine must not run this module.
"""

from __future__ import annotations

import datetime

import pytest

from checkwash.config import Config
from checkwash.contract import Contract
from checkwash.engine import FileChange, analyze
from checkwash.frontends.javascript.frontend import parse_javascript

HEAD = 'import { expect, it } from "vitest";\n'
ORACLE = "expect(withTax(10.005, 0.1)).toBe(11.01);"


def source(body: str) -> bytes:
    return (HEAD + 'it("rounds to cents", () => {\n' + body + "\n});\n").encode()


def outcome(before: str, after: str, path: str = "tests/cart.test.ts"):
    change = FileChange(path, "modified", source(before), source(after))
    _ir, findings, verdict = analyze(
        [change], Config(), Contract(), [], datetime.date(2026, 10, 11)
    )
    return verdict, [(finding.rule, finding.severity) for finding in findings]


@pytest.mark.parametrize("handler", [
    "catch (e) {}",
    "catch (e) { /* rounding differs between CI runners */ }",
    "catch {}",
    "catch (e) { return; }",
    "catch (e) { console.log(e); }",
])
def test_wrapping_an_unchanged_synchronous_assertion_blocks(handler):
    assert outcome(ORACLE, "try { " + ORACLE + " } " + handler) == (
        "block", [("BROAD_EXCEPT_ADDED", "high")]
    )


@pytest.mark.parametrize("handler", [
    "catch (e) { throw e; }",
    "catch (e) { throw new Error('failure'); }",
    "catch (e) { expect(e.message).toMatch(/invalid/); }",
])
def test_rethrow_or_active_assertion_is_not_swallowing(handler):
    parsed = parse_javascript(source("try { " + ORACLE + " } " + handler))
    assert parsed.swallowing_handlers == ()


def test_setup_only_try_does_not_wrap_the_assertion_after_it():
    assert outcome("setup(); " + ORACLE, "try { setup(); } catch {} " + ORACLE) == (
        "pass", []
    )


def test_unchanged_swallowing_handler_is_not_added():
    body = "try { " + ORACLE + " } catch {}"
    assert outcome(body, body) == ("pass", [])


def test_handler_formatting_and_binding_rename_are_not_an_addition():
    assert outcome(
        "try { " + ORACLE + " } catch (e) {}",
        "try {\n" + ORACLE + "\n} catch (error) { /* unchanged */ }",
    ) == ("pass", [])


def test_removing_a_rethrow_is_an_addition_without_a_new_catch_header():
    assert outcome(
        "try { " + ORACLE + " } catch (e) { throw e; }",
        "try { " + ORACLE + " } catch (e) {}",
    ) == ("block", [("BROAD_EXCEPT_ADDED", "high")])


@pytest.mark.parametrize("body", [
    "try { const unused = () => { " + ORACLE + " }; } catch {}",
    "try { register(() => { " + ORACLE + " }); } catch {}",
    "try { return; " + ORACLE + " } catch {}",
    "try { if (false) { " + ORACLE + " } } catch {}",
])
def test_nested_or_unreachable_assertion_does_not_donate_to_outer_try(body):
    assert parse_javascript(source(body)).swallowing_handlers == ()


@pytest.mark.parametrize("handler", [
    "catch (e) { const unused = () => { throw e; }; }",
    "catch (e) { const unused = () => { expect(e).toBeDefined(); }; }",
    "catch (e) { return; throw e; }",
    "catch (e) { if (false) { throw e; } }",
    "catch (expect) { expect('ignored').toBeDefined(); }",
])
def test_nested_dead_or_shadowed_catch_control_does_not_preserve_failure(handler):
    parsed = parse_javascript(source("try { " + ORACLE + " } " + handler))
    assert len(parsed.swallowing_handlers) == 1


def test_local_catch_inside_an_inline_callback_is_read():
    body = "rows.forEach(() => { try { " + ORACLE + " } catch {} });"
    assert len(parse_javascript(source(body)).swallowing_handlers) == 1


def test_async_callback_still_catches_a_synchronous_matcher_locally():
    text = HEAD + 'it("rounds", async () => { try { ' + ORACLE + " } catch {} });"
    assert len(parse_javascript(text.encode()).swallowing_handlers) == 1


@pytest.mark.parametrize("oracle", [
    "expect(value).resolves.toBe(1);",
    "expect(value).rejects.toThrow();",
])
def test_promise_completion_is_outside_this_synchronous_channel(oracle):
    assert parse_javascript(source("try { " + oracle + " } catch {}" )).swallowing_handlers == ()


def test_inert_text_is_not_a_handler():
    assert parse_javascript(source("const text = 'try { expect(x).toBe(1) } catch {}'; " + ORACLE)).swallowing_handlers == ()
