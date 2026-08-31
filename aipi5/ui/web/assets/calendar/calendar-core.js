/* Offline calendar arithmetic for AIPI5.
 *
 * The lunar converter beside this file is solarlunar 3.1.0 (ISC), pinned and
 * served locally.  This wrapper keeps policy -- holiday names, birthday
 * recurrence, Gregorian month layout -- out of both the library and the DOM.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./solarlunar.min.js").default);
  } else {
    root.AIPI5Calendar = factory(root.solarLunar && root.solarLunar.default);
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (lunarLib) {
  "use strict";

  const MONTHS = ["January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December"];
  const DAY_MS = 86400000;

  function localDate(year, month, day) {
    return new Date(year, month - 1, day, 12, 0, 0, 0);
  }

  function key(year, month, day) {
    return String(year).padStart(4, "0") + "-" + String(month).padStart(2, "0") +
           "-" + String(day).padStart(2, "0");
  }

  function dateKey(date) {
    return key(date.getFullYear(), date.getMonth() + 1, date.getDate());
  }

  function sameMonth(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth();
  }

  function daysInMonth(year, month) {
    return new Date(year, month, 0).getDate();
  }

  function shiftMonth(year, month, amount) {
    const date = new Date(year, month - 1 + amount, 1, 12);
    return {year: date.getFullYear(), month: date.getMonth() + 1};
  }

  function monthCells(year, month) {
    const first = localDate(year, month, 1);
    const start = new Date(first);
    start.setDate(1 - first.getDay());
    return Array.from({length: 42}, function (_, index) {
      const date = new Date(start);
      date.setDate(start.getDate() + index);
      return {
        year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate(),
        inMonth: date.getFullYear() === year && date.getMonth() + 1 === month,
        key: dateKey(date), date: date,
      };
    });
  }

  function solarToLunar(year, month, day) {
    if (!lunarLib) return null;
    try {
      const result = lunarLib.solar2lunar(year, month, day);
      return result && result !== -1 ? result : null;
    } catch (_) {
      return null;
    }
  }

  function lunarToSolar(year, month, day, leap) {
    if (!lunarLib) return null;
    try {
      const result = lunarLib.lunar2solar(year, month, day, Boolean(leap));
      return result && result !== -1 ? result : null;
    } catch (_) {
      return null;
    }
  }

  function lunarText(lunar) {
    if (!lunar) return "农历超出范围";
    return lunar.monthCn + lunar.dayCn;
  }

  function nthWeekday(year, month, weekday, nth) {
    const firstWeekday = localDate(year, month, 1).getDay();
    return 1 + ((weekday - firstWeekday + 7) % 7) + (nth - 1) * 7;
  }

  function lastWeekday(year, month, weekday) {
    const last = daysInMonth(year, month);
    const lastDay = localDate(year, month, last).getDay();
    return last - ((lastDay - weekday + 7) % 7);
  }

  function usHolidays(year) {
    const rows = [
      [1, 1, "New Year's Day", "New Year"],
      [1, nthWeekday(year, 1, 1, 3), "Martin Luther King Jr. Day", "MLK Day"],
      [2, nthWeekday(year, 2, 1, 3), "Presidents' Day", "Presidents' Day"],
      [5, lastWeekday(year, 5, 1), "Memorial Day", "Memorial Day"],
      [6, 19, "Juneteenth", "Juneteenth"],
      [7, 4, "Independence Day", "Independence Day"],
      [9, nthWeekday(year, 9, 1, 1), "Labor Day", "Labor Day"],
      [10, nthWeekday(year, 10, 1, 2),
       "Columbus Day / Indigenous Peoples' Day", "Columbus / Indigenous"],
      [11, 11, "Veterans Day", "Veterans Day"],
      [11, nthWeekday(year, 11, 4, 4), "Thanksgiving", "Thanksgiving"],
      [12, 25, "Christmas Day", "Christmas"],
    ];
    const result = {};
    for (const row of rows) {
      result[key(year, row[0], row[1])] = [{type: "us", name: row[2], short: row[3]}];
    }
    return result;
  }

  function chineseHolidays(year) {
    const result = {};
    function add(month, day, name, short) {
      const k = key(year, month, day);
      (result[k] || (result[k] = [])).push({type: "cn", name: name, short: short || name});
    }
    add(1, 1, "元旦");
    add(10, 1, "国庆节");

    // The six weeks on screen can cross a lunar-year boundary. Calculate from
    // the adjacent lunar years too, then keep only dates in this Gregorian one.
    const lunarRules = [
      [1, 1, "春节"], [1, 15, "元宵节"], [5, 5, "端午节"],
      [7, 7, "七夕节"], [8, 15, "中秋节"], [9, 9, "重阳节"], [12, 8, "腊八节"],
    ];
    for (let lunarYear = year - 1; lunarYear <= year + 1; lunarYear += 1) {
      for (const rule of lunarRules) {
        const solar = lunarToSolar(lunarYear, rule[0], rule[1], false);
        if (solar && solar.cYear === year) add(solar.cMonth, solar.cDay, rule[2]);
      }
      const nextNewYear = lunarToSolar(lunarYear + 1, 1, 1, false);
      if (nextNewYear) {
        const eve = new Date(nextNewYear.cYear, nextNewYear.cMonth - 1,
                             nextNewYear.cDay - 1, 12);
        if (eve.getFullYear() === year) add(eve.getMonth() + 1, eve.getDate(), "除夕");
      }
    }

    // 清明 follows the solar term, not a fixed lunar date.  The bundled
    // converter carries the term table, so this remains local and works in
    // the same 1900-2100 range as the rest of the lunar calculations.
    for (let day = 3; day <= 6; day += 1) {
      const lunar = solarToLunar(year, 4, day);
      if (lunar && lunar.term === "清明") add(4, day, "清明节");
    }
    return result;
  }

  function holidayMap(year) {
    const result = usHolidays(year);
    const chinese = chineseHolidays(year);
    for (const [k, rows] of Object.entries(chinese)) {
      result[k] = (result[k] || []).concat(rows);
    }
    return result;
  }

  function birthdayOccurrences(year, birthdays) {
    const result = {};
    function add(date, birthday) {
      if (!date || date.cYear !== year) return;
      const item = Object.assign({}, birthday, {
        occurrenceYear: year,
        age: birthday.year == null ? null : year - Number(birthday.year),
      });
      const k = key(year, date.cMonth, date.cDay);
      (result[k] || (result[k] = [])).push(item);
    }

    for (const birthday of birthdays || []) {
      if (birthday.calendar === "solar") {
        const date = localDate(year, Number(birthday.month), Number(birthday.day));
        if (date.getFullYear() === year && date.getMonth() + 1 === Number(birthday.month) &&
            date.getDate() === Number(birthday.day)) {
          add({cYear: year, cMonth: date.getMonth() + 1, cDay: date.getDate()}, birthday);
        }
      } else if (birthday.calendar === "lunar") {
        for (let lunarYear = year - 1; lunarYear <= year + 1; lunarYear += 1) {
          add(lunarToSolar(lunarYear, Number(birthday.month), Number(birthday.day),
                           Boolean(birthday.leap)), birthday);
        }
      }
    }
    return result;
  }

  function monthName(year, month) {
    return MONTHS[month - 1] + " " + year;
  }

  function fullDate(year, month, day) {
    return MONTHS[month - 1] + " " + day + ", " + year;
  }

  return {
    MONTHS: MONTHS, DAY_MS: DAY_MS, key: key, dateKey: dateKey,
    sameMonth: sameMonth, daysInMonth: daysInMonth, shiftMonth: shiftMonth,
    monthCells: monthCells, solarToLunar: solarToLunar, lunarToSolar: lunarToSolar,
    lunarText: lunarText, nthWeekday: nthWeekday, lastWeekday: lastWeekday,
    usHolidays: usHolidays, chineseHolidays: chineseHolidays,
    holidayMap: holidayMap, birthdayOccurrences: birthdayOccurrences,
    monthName: monthName, fullDate: fullDate,
  };
});
