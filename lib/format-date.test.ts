import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { formatDisplayDate, formatDisplayDateTime } from "./format-date.ts";

describe("formatDisplayDate", () => {
  it("formats a UTC timestamp with a stable day-month-year order", () => {
    assert.equal(formatDisplayDate("2026-09-14T12:00:00.000Z"), "2026-09-14");
  });

  it("does not follow the process default locale", () => {
    const value = "2026-09-14T12:00:00.000Z";
    const us = new Intl.DateTimeFormat("en-US").format(new Date(value));
    const gb = new Intl.DateTimeFormat("en-GB").format(new Date(value));
    assert.notEqual(us, gb);
    assert.equal(formatDisplayDate(value), "2026-09-14");
    assert.notEqual(formatDisplayDate(value), us);
  });

  it("returns the original string when the timestamp is invalid", () => {
    assert.equal(formatDisplayDate("not-a-date"), "not-a-date");
    assert.equal(formatDisplayDate(""), "");
  });
});

describe("formatDisplayDateTime", () => {
  it("formats a UTC timestamp without using the host locale", () => {
    assert.equal(formatDisplayDateTime("2026-09-14T15:04:00.000Z"), "2026-09-14 15:04 UTC");
  });
});
