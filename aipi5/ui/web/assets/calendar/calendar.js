(function () {
  "use strict";

  const C = window.AIPI5Calendar;
  const AUTO_RETURN_MS = 5 * 60 * 1000;
  const page = document.getElementById("page-calendar");
  if (!C || !page) return;

  const $ = (id) => document.getElementById(id);
  const state = {
    active: false,
    year: 0,
    month: 0,
    selected: "",
    today: "",
    birthdays: [],
    holidayCache: new Map(),
    returnTimer: 0,
    midnightTimer: 0,
    editing: "",
    deleting: "",
  };

  function calendarNow() {
    return typeof window.now === "function" ? window.now() : new Date();
  }

  function parseKey(value) {
    const bits = value.split("-").map(Number);
    return {year: bits[0], month: bits[1], day: bits[2]};
  }

  function holidays(year) {
    if (!state.holidayCache.has(year)) state.holidayCache.set(year, C.holidayMap(year));
    return state.holidayCache.get(year);
  }

  function birthdayMap(year) {
    return C.birthdayOccurrences(year, state.birthdays);
  }

  function eventData(year) {
    return {holidays: holidays(year), birthdays: birthdayMap(year)};
  }

  function displayedIsCurrent() {
    const today = calendarNow();
    return state.year === today.getFullYear() && state.month === today.getMonth() + 1;
  }

  function publishTimerDiagnostics() {
    page.dataset.inactivityTimers = state.returnTimer ? "1" : "0";
    page.dataset.midnightTimers = state.midnightTimer ? "1" : "0";
  }

  function clearReturnTimer() {
    if (state.returnTimer) window.clearTimeout(state.returnTimer);
    state.returnTimer = 0;
    publishTimerDiagnostics();
  }

  function scheduleReturn() {
    clearReturnTimer();
    if (!state.active || displayedIsCurrent()) return;
    state.returnTimer = window.setTimeout(function () {
      state.returnTimer = 0;
      publishTimerDiagnostics();
      goToday();
    }, AUTO_RETURN_MS);
    publishTimerDiagnostics();
  }

  function noteInteraction() {
    if (state.active) scheduleReturn();
  }

  function clearMidnightTimer() {
    if (state.midnightTimer) window.clearTimeout(state.midnightTimer);
    state.midnightTimer = 0;
    publishTimerDiagnostics();
  }

  function scheduleMidnight() {
    clearMidnightTimer();
    if (!state.active) return;
    const nowValue = calendarNow();
    const next = new Date(nowValue.getFullYear(), nowValue.getMonth(),
                          nowValue.getDate() + 1, 0, 0, 1, 0);
    const delay = Math.max(1000, next.getTime() - nowValue.getTime());
    state.midnightTimer = window.setTimeout(handleDateChange, delay);
    publishTimerDiagnostics();
  }

  function handleDateChange() {
    state.midnightTimer = 0;
    publishTimerDiagnostics();
    if (!state.active) return;
    const previous = parseKey(state.today);
    const current = calendarNow();
    const newKey = C.dateKey(current);
    const followedCurrent = state.year === previous.year && state.month === previous.month;
    state.today = newKey;
    if (followedCurrent) {
      state.year = current.getFullYear();
      state.month = current.getMonth() + 1;
      state.selected = newKey;
    }
    state.holidayCache.clear();
    render();
    scheduleReturn();
    scheduleMidnight();
  }

  function goToday() {
    const today = calendarNow();
    state.year = today.getFullYear();
    state.month = today.getMonth() + 1;
    state.today = C.dateKey(today);
    state.selected = state.today;
    clearReturnTimer();
    render();
  }

  function moveMonth(amount) {
    const shifted = C.shiftMonth(state.year, state.month, amount);
    state.year = shifted.year;
    state.month = shifted.month;
    state.selected = C.key(state.year, state.month, 1);
    render();
    scheduleReturn();
  }

  function create(className, text) {
    const node = document.createElement("div");
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function selectDate(parts) {
    if (parts.year !== state.year || parts.month !== state.month) {
      state.year = parts.year;
      state.month = parts.month;
    }
    state.selected = C.key(parts.year, parts.month, parts.day);
    render();
    scheduleReturn();
  }

  function renderGrid(data) {
    const grid = $("cal-grid");
    const fragment = document.createDocumentFragment();
    for (const cell of C.monthCells(state.year, state.month)) {
      const lunar = C.solarToLunar(cell.year, cell.month, cell.day);
      const holidayRows = data.holidays[cell.key] || [];
      const birthdayRows = data.birthdays[cell.key] || [];
      const button = document.createElement("button");
      button.type = "button";
      button.className = "cal-day" + (cell.inMonth ? "" : " outside") +
                         (cell.date.getDay() === 0 || cell.date.getDay() === 6 ? " weekend" : "") +
                         (cell.key === state.today ? " today" : "") +
                         (cell.key === state.selected ? " selected" : "");
      button.setAttribute("aria-label", C.fullDate(cell.year, cell.month, cell.day));
      if (!cell.inMonth) {
        button.disabled = true;
        button.tabIndex = -1;
        button.setAttribute("aria-hidden", "true");
      }
      button.appendChild(create("cal-number", String(cell.day)));
      button.appendChild(create("cal-lunar", C.lunarText(lunar)));

      const shown = holidayRows.concat(birthdayRows).slice(0, 2);
      for (const event of shown) {
        const birthday = !event.type;
        button.appendChild(create(birthday ? "cal-event birthday" : "cal-event holiday",
                                  birthday ? event.name + "生日" : event.short));
      }
      const hidden = holidayRows.length + birthdayRows.length - shown.length;
      if (hidden > 0) button.appendChild(create("cal-more", "+" + hidden));
      button.addEventListener("click", function () { selectDate(cell); });
      fragment.appendChild(button);
    }
    grid.replaceChildren(fragment);
  }

  function dayLabel(k) {
    const date = parseKey(k);
    return C.MONTHS[date.month - 1].slice(0, 3) + " " + date.day;
  }

  function appendEmpty(parent, text) {
    parent.appendChild(create("cal-panel-empty", text));
  }

  function appendEventRow(parent, event, k, kind) {
    const row = create("cal-panel-row " + kind);
    const date = create("cal-panel-date", dayLabel(k));
    const body = create("cal-panel-event");
    const title = create("cal-panel-name", kind === "birthday" ? event.name + "'s Birthday" : event.name);
    body.appendChild(title);
    if (kind === "birthday") {
      const details = [];
      if (event.calendar === "lunar") details.push("农历 " +
        (event.leap ? "闰" : "") + lunarMonthName(event.month) + lunarDayName(event.day));
      if (event.age != null && event.age >= 0) details.push("Turns " + event.age);
      if (event.note) details.push(event.note);
      if (details.length) body.appendChild(create("cal-panel-note", details.join(" · ")));
    }
    row.append(date, body);
    row.addEventListener("click", function () { selectDate(parseKey(k)); });
    parent.appendChild(row);
  }

  function renderSelected(data) {
    const box = $("cal-selected-detail");
    box.replaceChildren();
    const selected = parseKey(state.selected);
    const lunar = C.solarToLunar(selected.year, selected.month, selected.day);
    box.appendChild(create("cal-selected-date", C.fullDate(selected.year, selected.month, selected.day)));
    box.appendChild(create("cal-selected-lunar", "农历" + C.lunarText(lunar)));
    const hs = data.holidays[state.selected] || [];
    const bs = data.birthdays[state.selected] || [];
    for (const holiday of hs) box.appendChild(create("cal-detail-event holiday", holiday.name));
    for (const birthday of bs) box.appendChild(create("cal-detail-event birthday", birthday.name + "'s Birthday"));
    if (!hs.length && !bs.length) box.appendChild(create("cal-panel-empty", "No events on this date"));
  }

  function renderSection(id, rows, kind) {
    const target = $(id);
    target.replaceChildren();
    if (!rows.length) {
      appendEmpty(target, "None this month");
      return;
    }
    for (const row of rows) appendEventRow(target, row.event, row.key, kind);
  }

  function renderPanel(data) {
    renderSelected(data);
    const prefix = C.key(state.year, state.month, 1).slice(0, 7) + "-";
    const us = [], cn = [], birthdays = [];
    for (const [k, rows] of Object.entries(data.holidays)) {
      if (!k.startsWith(prefix)) continue;
      for (const event of rows) (event.type === "us" ? us : cn).push({key: k, event: event});
    }
    for (const [k, rows] of Object.entries(data.birthdays)) {
      if (!k.startsWith(prefix)) continue;
      for (const event of rows) birthdays.push({key: k, event: event});
    }
    const byDate = (a, b) => a.key.localeCompare(b.key) || a.event.name.localeCompare(b.event.name);
    us.sort(byDate); cn.sort(byDate); birthdays.sort(byDate);
    renderSection("cal-us-events", us, "holiday");
    renderSection("cal-cn-events", cn, "holiday");
    renderSection("cal-birthday-events", birthdays, "birthday");
    renderBirthdaySettings();
  }

  function render() {
    if (!state.year) return;
    $("cal-month-title").textContent = C.monthName(state.year, state.month);
    $("cal-panel-title").textContent = C.monthName(state.year, state.month);
    const data = eventData(state.year);
    renderGrid(data);
    renderPanel(data);
  }

  function lunarMonthName(month) {
    return ["正月", "二月", "三月", "四月", "五月", "六月", "七月", "八月",
            "九月", "十月", "冬月", "腊月"][Number(month) - 1] || "";
  }

  function lunarDayName(day) {
    const names = ["初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
                   "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
                   "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十"];
    return names[Number(day) - 1] || "";
  }

  function birthdayDateText(birthday) {
    let text;
    if (birthday.calendar === "lunar") {
      text = "农历 " + (birthday.leap ? "闰" : "") +
             lunarMonthName(birthday.month) + lunarDayName(birthday.day);
    } else {
      text = C.MONTHS[Number(birthday.month) - 1] + " " + Number(birthday.day);
    }
    if (birthday.year != null) text += ", " + birthday.year;
    return text;
  }

  function renderBirthdaySettings() {
    const target = $("birthday-settings-list");
    target.replaceChildren();
    const birthdays = state.birthdays.slice().sort(function (a, b) {
      return a.name.localeCompare(b.name) || Number(a.month) - Number(b.month) ||
             Number(a.day) - Number(b.day);
    });
    if (!birthdays.length) {
      appendEmpty(target, "No birthdays saved");
      return;
    }
    for (const birthday of birthdays) {
      const row = create("birthday-setting-row");
      const details = create("birthday-setting-details");
      details.appendChild(create("birthday-setting-name", birthday.name));
      details.appendChild(create("birthday-setting-date", birthdayDateText(birthday)));
      if (birthday.note) details.appendChild(create("birthday-setting-note", birthday.note));

      const actions = create("birthday-setting-actions");
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "Edit";
      edit.addEventListener("click", function () { openBirthday(birthday); });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Delete";
      remove.className = "danger";
      remove.addEventListener("click", function () { confirmDelete(birthday); });
      actions.append(edit, remove);
      row.append(details, actions);
      target.appendChild(row);
    }
  }

  function openBirthdaySettings() {
    noteInteraction();
    renderBirthdaySettings();
    $("birthday-settings-dialog").showModal();
  }

  function fillSelect(select, rows) {
    select.replaceChildren();
    for (const row of rows) {
      const option = document.createElement("option");
      option.value = String(row.value);
      option.textContent = row.label;
      select.appendChild(option);
    }
  }

  function updateBirthdayFields() {
    const lunar = $("birthday-calendar").value === "lunar";
    const monthRows = Array.from({length: 12}, (_, i) => ({
      value: i + 1, label: lunar ? lunarMonthName(i + 1) : C.MONTHS[i],
    }));
    const currentMonth = Number($("birthday-month").value || 1);
    const currentDay = Number($("birthday-day").value || 1);
    fillSelect($("birthday-month"), monthRows);
    $("birthday-month").value = String(currentMonth);
    const maximum = lunar ? 30 : C.daysInMonth(2024, currentMonth);
    fillSelect($("birthday-day"), Array.from({length: maximum}, (_, i) => ({
      value: i + 1, label: lunar ? lunarDayName(i + 1) : String(i + 1),
    })));
    $("birthday-day").value = String(Math.min(currentDay, maximum));
    $("birthday-leap-wrap").hidden = !lunar;
  }

  function openBirthday(existing) {
    noteInteraction();
    state.editing = existing ? existing.id : "";
    $("birthday-dialog-title").textContent = existing ? "Edit Birthday" : "Add Birthday";
    $("birthday-name").value = existing ? existing.name : "";
    $("birthday-calendar").value = existing ? existing.calendar : "solar";
    const selected = parseKey(state.selected);
    const lunar = C.solarToLunar(selected.year, selected.month, selected.day);
    $("birthday-month").value = String(existing ? existing.month : selected.month);
    $("birthday-day").value = String(existing ? existing.day : selected.day);
    updateBirthdayFields();
    if (!existing && $("birthday-calendar").value === "lunar" && lunar) {
      $("birthday-month").value = String(lunar.lMonth);
      $("birthday-day").value = String(lunar.lDay);
    }
    $("birthday-leap").checked = Boolean(existing && existing.leap);
    $("birthday-year").value = existing && existing.year != null ? String(existing.year) : "";
    $("birthday-note").value = existing ? existing.note || "" : "";
    $("birthday-error").textContent = "";
    $("birthday-dialog").showModal();
  }

  async function saveBirthday(event) {
    event.preventDefault();
    noteInteraction();
    const birthday = {
      id: state.editing,
      name: $("birthday-name").value,
      calendar: $("birthday-calendar").value,
      month: Number($("birthday-month").value),
      day: Number($("birthday-day").value),
      leap: $("birthday-calendar").value === "lunar" && $("birthday-leap").checked,
      year: $("birthday-year").value,
      note: $("birthday-note").value,
    };
    try {
      const result = await postBirthdays({action: "save", birthday: birthday});
      state.birthdays = result.birthdays || [];
      $("birthday-dialog").close();
      render();
    } catch (error) {
      $("birthday-error").textContent = error.message;
    }
  }

  function confirmDelete(birthday) {
    noteInteraction();
    state.deleting = birthday.id;
    $("birthday-delete-text").textContent = "Delete " + birthday.name + "'s birthday?";
    $("birthday-delete-dialog").showModal();
  }

  async function deleteBirthday() {
    noteInteraction();
    try {
      const result = await postBirthdays({action: "delete", id: state.deleting});
      state.birthdays = result.birthdays || [];
      $("birthday-delete-dialog").close();
      render();
    } catch (error) {
      $("birthday-delete-text").textContent = error.message;
    }
  }

  async function postBirthdays(payload) {
    const response = await fetch("/api/calendar/birthdays", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "Could not save birthday");
    return result;
  }

  async function loadBirthdays() {
    try {
      const response = await fetch("/api/calendar/birthdays", {cache: "no-store"});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not load birthdays");
      state.birthdays = Array.isArray(result.birthdays) ? result.birthdays : [];
      if (state.active) render();
    } catch (error) {
      $("cal-storage-note").textContent = "Birthday storage is unavailable";
    }
  }

  window.startCalendar = function () {
    if (state.active) return;
    state.active = true;
    $("cal-storage-note").textContent = "";
    goToday();
    scheduleMidnight();
    loadBirthdays();
  };

  window.stopCalendar = function () {
    state.active = false;
    clearReturnTimer();
    clearMidnightTimer();
    if ($("birthday-dialog").open) $("birthday-dialog").close();
    if ($("birthday-delete-dialog").open) $("birthday-delete-dialog").close();
    if ($("birthday-settings-dialog").open) $("birthday-settings-dialog").close();
  };

  window.AIPI5CalendarController = {
    diagnostics: function () {
      return {active: state.active, inactivityTimers: state.returnTimer ? 1 : 0,
              midnightTimers: state.midnightTimer ? 1 : 0,
              displayed: {year: state.year, month: state.month}};
    },
    handleDateChange: handleDateChange,
    goToday: goToday,
  };

  $("cal-prev").addEventListener("click", function () { moveMonth(-1); });
  $("cal-next").addEventListener("click", function () { moveMonth(1); });
  $("cal-today").addEventListener("click", goToday);
  $("cal-birthday-settings").addEventListener("click", openBirthdaySettings);
  $("birthday-settings-add").addEventListener("click", function () { openBirthday(null); });
  $("birthday-settings-close").addEventListener("click", function () {
    $("birthday-settings-dialog").close();
  });
  $("birthday-settings-dismiss").addEventListener("click", function () {
    $("birthday-settings-dialog").close();
  });
  $("birthday-calendar").addEventListener("change", updateBirthdayFields);
  $("birthday-month").addEventListener("change", updateBirthdayFields);
  $("birthday-form").addEventListener("submit", saveBirthday);
  $("birthday-cancel").addEventListener("click", function () { $("birthday-dialog").close(); });
  $("birthday-delete-cancel").addEventListener("click", function () {
    $("birthday-delete-dialog").close();
  });
  $("birthday-delete-confirm").addEventListener("click", deleteBirthday);

  // Installed once for the lifetime of the document. Entering/leaving the page
  // only creates and clears the two timeout handles above, so listeners cannot
  // accumulate across repeated visits.
  page.addEventListener("pointerdown", noteInteraction, {passive: true});
  page.addEventListener("input", noteInteraction, {passive: true});
  $("cal-panel-scroll").addEventListener("scroll", noteInteraction, {passive: true});
})();
