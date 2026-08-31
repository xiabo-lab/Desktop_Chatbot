import assert from "node:assert/strict";
import {createRequire} from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const C = require("../aipi5/ui/web/assets/calendar/calendar-core.js");

test("month navigation crosses years in both directions", () => {
  assert.deepEqual(C.shiftMonth(2026, 12, 1), {year: 2027, month: 1});
  assert.deepEqual(C.shiftMonth(2027, 1, -1), {year: 2026, month: 12});
  assert.deepEqual(C.shiftMonth(2026, 8, 48), {year: 2030, month: 8});
  assert.deepEqual(C.shiftMonth(2026, 8, -48), {year: 2022, month: 8});
  assert.equal(C.monthCells(2026, 8).length, 42);
});

test("known Gregorian and lunar dates convert in both directions", () => {
  const midAutumn = C.solarToLunar(2026, 9, 25);
  assert.deepEqual([midAutumn.lYear, midAutumn.lMonth, midAutumn.lDay, midAutumn.isLeap],
                   [2026, 8, 15, false]);
  assert.equal(C.lunarText(midAutumn), "八月十五");
  const solar = C.lunarToSolar(2026, 8, 15, false);
  assert.deepEqual([solar.cYear, solar.cMonth, solar.cDay], [2026, 9, 25]);

  const leap = C.solarToLunar(2020, 5, 23);
  assert.deepEqual([leap.lYear, leap.lMonth, leap.lDay, leap.isLeap],
                   [2020, 4, 1, true]);
  assert.equal(C.lunarText(leap), "闰四月初一");
  const leapSolar = C.lunarToSolar(2020, 4, 1, true);
  assert.deepEqual([leapSolar.cYear, leapSolar.cMonth, leapSolar.cDay], [2020, 5, 23]);
});

test("US fixed and moving holiday rules work across years", () => {
  const h2026 = C.usHolidays(2026);
  assert.equal(h2026["2026-01-01"][0].name, "New Year's Day");
  assert.equal(h2026["2026-01-19"][0].name, "Martin Luther King Jr. Day");
  assert.equal(h2026["2026-05-25"][0].name, "Memorial Day");
  assert.equal(h2026["2026-09-07"][0].name, "Labor Day");
  assert.equal(h2026["2026-11-26"][0].name, "Thanksgiving");
  assert.equal(C.usHolidays(2027)["2027-11-25"][0].name, "Thanksgiving");
});

test("Chinese holidays use lunar dates and Chinese names", () => {
  const h2026 = C.chineseHolidays(2026);
  assert.equal(h2026["2026-01-01"][0].name, "元旦");
  assert.ok(h2026["2026-02-17"].some((event) => event.name === "春节"));
  assert.ok(h2026["2026-03-03"].some((event) => event.name === "元宵节"));
  assert.ok(h2026["2026-06-19"].some((event) => event.name === "端午节"));
  assert.ok(h2026["2026-09-25"].some((event) => event.name === "中秋节"));
  assert.ok(Object.values(h2026).flat().some((event) => event.name === "清明节"));
  for (const event of Object.values(h2026).flat()) {
    assert.doesNotMatch(event.name, /Chun|Jie|Zhong|Qiu/);
  }
});

test("solar and lunar birthdays recur without changing calendar type", () => {
  const birthdays = [
    {id: "john", name: "John", calendar: "solar", month: 4, day: 12,
     leap: false, year: 1980, note: ""},
    {id: "mom", name: "妈妈", calendar: "lunar", month: 8, day: 15,
     leap: false, year: null, note: ""},
  ];
  assert.equal(C.birthdayOccurrences(2026, birthdays)["2026-04-12"][0].age, 46);
  assert.equal(C.birthdayOccurrences(2026, birthdays)["2026-09-25"][0].name, "妈妈");
  assert.equal(C.birthdayOccurrences(2027, birthdays)["2027-09-15"][0].name, "妈妈");
  assert.equal(C.birthdayOccurrences(2028, birthdays)["2028-10-03"][0].name, "妈妈");
});

test("leap-month birthdays occur only in matching leap months", () => {
  const birthday = [{id: "leap", name: "Leap", calendar: "lunar", month: 4,
                     day: 1, leap: true, year: null, note: ""}];
  assert.equal(C.birthdayOccurrences(2020, birthday)["2020-05-23"][0].name, "Leap");
  assert.deepEqual(C.birthdayOccurrences(2021, birthday), {});
});
