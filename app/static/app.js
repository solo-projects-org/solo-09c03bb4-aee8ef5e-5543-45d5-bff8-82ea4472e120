// Archive page.  Plain DOM, no framework, so the demo has no build step.

const state = {
  channels: [],
  active: null,
  archive: null,
  loading: false,
  demo: null,
};

const $ = (selector, root = document) => root.querySelector(selector);

const STATUS_LABEL = {
  captured: "已取",
  pending: "待补",
  missing: "缺失",
};

/* ------------------------------------------------------------------ */
/* Small helpers                                                      */
/* ------------------------------------------------------------------ */
function fmtUtc(iso) {
  // 2026-11-20T00:01:06.000Z -> 11-20 00:01:06
  if (!iso) return "";
  return iso.slice(5, 19).replace("T", " ");
}

function fmtUtcFull(iso) {
  return iso ? iso.slice(0, 19) + "Z" : "";
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

let toastTimer = null;
function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.style.borderColor = isError ? "var(--missing)" : "var(--accent)";
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 5000);
}

/* ------------------------------------------------------------------ */
/* URL state: channel + UTC range is the archive identity.            */
/* ------------------------------------------------------------------ */
function readUrl() {
  const params = new URLSearchParams(location.search);
  return {
    channel: params.get("channel"),
    from: params.get("from"),
    to: params.get("to"),
  };
}

function writeUrl({ channel, from, to }) {
  const params = new URLSearchParams();
  if (channel) params.set("channel", channel);
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  const query = params.toString();
  history.replaceState(null, "", query ? `?${query}` : location.pathname);
}

function toInputValue(iso) {
  return iso ? iso.slice(0, 19) + "Z" : "";
}

function fromInputValue(value) {
  if (!value) return null;
  const trimmed = value.trim();
  return /[Zz]$/.test(trimmed) ? trimmed.toUpperCase() : trimmed + "Z";
}

function demoChannel() {
  if (!state.demo) return null;
  return state.demo.channels.find((item) => item.id === state.active) || null;
}

/* ------------------------------------------------------------------ */
/* Rendering                                                          */
/* ------------------------------------------------------------------ */
function renderTabs() {
  $("#tabs").replaceChildren(
    ...state.channels.map((channel) =>
      el("button", {
        class: `tab ${channel.id === state.active ? "active" : ""}`,
        text: channel.name,
        onclick: () => switchChannel(channel.id),
      })
    )
  );
}

function renderCards(view) {
  const counts = view.counts;
  const cards = [
    ["已取片段", counts.captured, "含空字幕片"],
    ["空字幕片段", counts.empty_captions, "取到但无字幕"],
    ["待补片段", counts.pending, "已见未取"],
    ["缺失片段", counts.missing, "滑出窗口未取"],
    ["区间字幕条数", counts.cues_in_range, "合并后"],
    ["区间缺口数", counts.gaps_in_range, "待补+缺失"],
  ];
  $("#cards").replaceChildren(
    ...cards.map(([key, value, hint]) =>
      el("div", { class: "card" }, [
        el("div", { class: "k", text: key }),
        el("div", { class: "v", text: String(value) }),
        el("div", { class: "k", text: hint }),
      ])
    )
  );
}

function renderSegments(view) {
  const strip = $("#strip");
  if (!view.segments.length) {
    strip.replaceChildren(
      el("div", { class: "empty-state", text: "该区间还没有任何片段记录，先点“采集本频道”。" })
    );
    return;
  }
  strip.replaceChildren(
    ...view.segments.map((segment) => {
      const isEmpty = segment.status === "captured" && segment.captions_empty;
      const tag =
        segment.status === "captured"
          ? isEmpty
            ? "已取·空字幕"
            : "已取"
          : STATUS_LABEL[segment.status];
      return el(
        "div",
        {
          class: `seg ${segment.status} ${isEmpty ? "empty" : ""}`,
          title: `#${segment.seq}\n${fmtUtcFull(segment.start_utc)} → ${fmtUtcFull(segment.end_utc)}${
            segment.error ? `\n上次错误：${segment.error}` : ""
          }`,
        },
        [
          el("div", { class: "seq", text: `#${segment.seq}` }),
          el("div", { class: "utc", text: fmtUtc(segment.start_utc) }),
          el("span", { class: "tag", text: tag }),
        ]
      );
    })
  );
}

function renderCoverage(view) {
  const runs = $("#runs");
  if (!view.coverage.length) {
    runs.replaceChildren(
      el("div", { class: "empty-state", text: "该区间内没有连续已取覆盖。" })
    );
    return;
  }
  runs.replaceChildren(
    ...view.coverage.map((span) =>
      el("div", { class: "run" }, [
        el("span", {
          text: `${fmtUtcFull(span.start_utc)} → ${fmtUtcFull(span.end_utc)}`,
        }),
        el("span", {
          class: "badge",
          text: `#${span.seq_start}–#${span.seq_end}${span.complete ? "" : " ·区间截断"}`,
        }),
      ])
    )
  );
}

function renderCues(view) {
  const body = $("#cue-body");
  if (!view.cues.length) {
    body.replaceChildren(
      el("tr", {}, [
        el("td", {
          class: "empty-state",
          colspan: "3",
          text: "该区间没有已归档字幕。",
        }),
      ])
    );
    return;
  }
  body.replaceChildren(
    ...view.cues.map((cue) =>
      el("tr", {}, [
        el("td", { class: "time" }, [
          el("div", { text: cue.start_utc }),
          el("div", { text: cue.end_utc }),
        ]),
        el("td", { class: "text" }, [
          cue.text,
          cue.merged
            ? el("span", {
                class: "pill merged",
                text: `跨片合并 #${cue.seqs.join("/#")}`,
              })
            : null,
        ]),
        el("td", { class: "time", text: cue.seqs.map((seq) => `#${seq}`).join(", ") }),
      ])
    )
  );
}

function renderGaps(view) {
  const node = $("#gaps");
  if (!view.gaps.length) {
    node.replaceChildren(
      el("div", { class: "empty-state", text: "该区间没有待补或缺失的片段。" })
    );
    return;
  }
  node.replaceChildren(
    el(
      "ul",
      { class: "gap-list" },
      view.gaps.map((gap) =>
        el("li", {}, [
          el("span", {
            class: "mono",
            style: "color:var(--missing);font-weight:600",
            text: STATUS_LABEL[gap.status],
          }),
          el("span", { class: "mono", text: `#${gap.seq}` }),
          el("span", {
            class: "mono",
            text: `${gap.start_utc} → ${gap.end_utc}`,
          }),
          gap.status === "pending"
            ? el("span", {
                class: "hint",
                text: "已见未取：仍在直播窗口内，稍后重采即会尝试补齐。",
              })
            : el("span", { class: "hint", text: "已滑出窗口：重采时若源仍可提供则会补齐。" }),
          gap.error ? el("span", { class: "hint", text: `上次错误：${gap.error}` }) : null,
        ])
      )
    )
  );
}

function renderArchive() {
  const view = state.archive;
  const meta = $("#range-meta");
  if (!view) {
    meta.textContent = "";
    return;
  }
  const bounds = view.bounds
    ? `已见范围 ${view.bounds.start_utc} → ${view.bounds.end_utc}`
    : "尚无片段记录";
  const scope = view.default_range ? "全时段" : "筛选区间";
  meta.textContent = `${scope} ${view.range.from_utc} → ${view.range.to_utc} · ${bounds}`;
  renderCards(view);
  renderSegments(view);
  renderCoverage(view);
  renderCues(view);
  renderGaps(view);

  if (document.activeElement !== $("#from")) {
    $("#from").value = view.default_range ? "" : toInputValue(view.range.from_utc);
  }
  if (document.activeElement !== $("#to")) {
    $("#to").value = view.default_range ? "" : toInputValue(view.range.to_utc);
  }
}

function renderDemo() {
  const node = $("#demo-state");
  const demo = state.demo;
  if (!demo) {
    node.textContent = "演示源不可用（检查 origin 容器是否已启动）。";
    return;
  }
  const channel = demoChannel();
  if (!channel) {
    node.textContent = "";
    return;
  }
  const parts = [
    `窗口 #${channel.window_start}–#${channel.window_end - 1}`,
    `UTC ${channel.window_start_utc} → ${channel.window_end_utc}`,
    `取片延迟 ${demo.latency_s}s`,
    channel.frozen ? "列表已冻结" : null,
    channel.stale_by ? `列表陈旧 ${channel.stale_by} 片` : null,
    channel.gaps.length ? `缺口 #${channel.gaps.join(", #")}` : "无缺口",
  ].filter(Boolean);
  node.textContent = parts.join(" · ");
}

/* ------------------------------------------------------------------ */
/* Data flow                                                          */
/* ------------------------------------------------------------------ */
async function loadArchive({ preserveInputs = false } = {}) {
  if (!state.active) return;
  const params = new URLSearchParams();
  if (preserveInputs) {
    const from = fromInputValue($("#from").value);
    const to = fromInputValue($("#to").value);
    if (from) params.set("from", from);
    if (to) params.set("to", to);
  }
  const query = params.toString();
  const view = await api(
    `/api/channels/${encodeURIComponent(state.active)}/archive${query ? `?${query}` : ""}`
  );
  state.archive = view;
  if (view.default_range) {
    writeUrl({ channel: state.active });
  } else {
    writeUrl({ channel: state.active, from: view.range.from_utc, to: view.range.to_utc });
  }
  renderArchive();
  renderDemo();
}

async function refreshDemo() {
  try {
    state.demo = await api("/api/demo/state");
  } catch (_) {
    state.demo = null;
  }
  renderDemo();
}

async function switchChannel(channelId) {
  state.active = channelId;
  renderTabs();
  $("#from").value = "";
  $("#to").value = "";
  // Reset the live window for a newly selected channel.
  if (state.demo) {
    await refreshDemo();
  }
  await loadArchive();
}

function captureSummary(result) {
  const bits = [
    `取片 ${result.fetched}`,
    `入档 ${result.captured}`,
    result.empty_segments ? `空字幕 ${result.empty_segments}` : null,
    result.failed ? `失败 ${result.failed}` : null,
    result.unresolved_after ? `未补齐 ${result.unresolved_after}` : null,
    result.marked_missing.length ? `新标缺失 #${result.marked_missing.join(", #")}` : null,
    result.stale_playlist ? "旧列表未回退状态" : null,
  ].filter(Boolean);
  if (result.notes && result.notes.length) {
    bits.push(`说明：${result.notes.join("；")}`);
  }
  return bits.join(" · ");
}

async function runCapture(button, channelId = state.active, { quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  const label = button ? button.textContent : null;
  if (button) {
    button.disabled = true;
    button.textContent = "采集进行中…";
  }
  try {
    const payload = await api(`/api/channels/${encodeURIComponent(channelId)}/capture`, {
      method: "POST",
      body: "{}",
    });
    const result = payload.result;
    if (!quiet || result.failed) {
      toast(`采集完成：${captureSummary(result)}`, result.failed > 0 && result.captured === 0);
    }
    return result;
  } catch (error) {
    toast(`采集失败：${error.message}`, true);
    return null;
  } finally {
    state.loading = false;
    if (button) {
      button.disabled = false;
      button.textContent = label;
    }
    await loadArchive({ preserveInputs: true });
    await refreshDemo();
  }
}

/* ------------------------------------------------------------------ */
/* Demo origin controls                                               */
/* ------------------------------------------------------------------ */
async function demoPost(path, body) {
  try {
    state.demo = await api(`/api/demo/${path}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderDemo();
    return true;
  } catch (error) {
    toast(`演示源操作失败：${error.message}`, true);
    return false;
  }
}

function bindDemoControls() {
  $("#demo-advance-1").addEventListener("click", async () => {
    if (await demoPost("advance", { channel: state.active, segments: 1 })) {
      await runCapture($("#capture"), state.active);
    }
  });
  $("#demo-advance-8").addEventListener("click", async () => {
    if (await demoPost("advance", { channel: state.active, segments: 8 })) {
      await runCapture($("#capture"), state.active);
    }
  });
  $("#demo-gap-add").addEventListener("click", async () => {
    const channel = demoChannel();
    if (!channel) return toast("演示源不可用", true);
    // Target the segment that will slide into the window next, so it has not
    // been captured yet and the next capture must leave it pending.
    const target = channel.window_end;
    if (channel.gaps.includes(target)) {
      return toast(`#${target} 已经是缺口`);
    }
    if (await demoPost("gap", { channel: state.active, seq: target, missing: true })) {
      toast(`已让下一片 #${target} 取片返回 404；推进窗口并采集后会标为待补`);
      await runCapture($("#capture"), state.active);
    }
  });
  $("#demo-gap-heal").addEventListener("click", async () => {
    const channel = demoChannel();
    if (!channel) return toast("演示源不可用", true);
    if (!channel.gaps.length) return toast("当前没有缺口可补");
    for (const seq of channel.gaps) {
      await demoPost("gap", { channel: state.active, seq, missing: false });
    }
    toast(`已恢复 #${channel.gaps.join(", #")} 取片，重采后补齐`);
    await runCapture($("#capture"), state.active);
  });
  $("#demo-latency").addEventListener("change", async (event) => {
    const seconds = Number(event.target.value);
    if (await demoPost("latency", { seconds })) {
      toast(`取片延迟设为 ${seconds}s`);
    }
  });
  $("#demo-stale").addEventListener("click", async () => {
    if (await demoPost("stale", { channel: state.active, segments: 8 })) {
      toast("演示源改为返回 8 片前的旧列表，重采不会回退任何已取片段");
    }
  });
  $("#demo-stale-clear").addEventListener("click", async () => {
    if (await demoPost("stale", { channel: state.active, segments: 0 })) {
      toast("演示列表恢复实时");
    }
  });
}

/* ------------------------------------------------------------------ */
/* Boot                                                               */
/* ------------------------------------------------------------------ */
async function boot() {
  const url = readUrl();
  const payload = await api("/api/channels");
  state.channels = payload.channels;
  const known = state.channels.map((channel) => channel.id);
  state.active = known.includes(url.channel) ? url.channel : known[0] || null;
  if (!state.active) {
    $("#range-meta").textContent = "没有配置频道。";
    return;
  }
  renderTabs();
  $("#from").value = url.from ? toInputValue(url.from) : "";
  $("#to").value = url.to ? toInputValue(url.to) : "";

  $("#capture").addEventListener("click", () => runCapture($("#capture")));
  $("#capture-all").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    for (const channel of state.channels) {
      await runCapture(null, channel.id, { quiet: channel.id !== state.active });
    }
    button.disabled = false;
  });
  $("#range-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await loadArchive({ preserveInputs: true });
    } catch (error) {
      toast(`查询失败：${error.message}`, true);
    }
  });
  $("#range-clear").addEventListener("click", async () => {
    $("#from").value = "";
    $("#to").value = "";
    await loadArchive();
  });

  bindDemoControls();

  await refreshDemo();
  try {
    await loadArchive({ preserveInputs: true });
  } catch (error) {
    toast(`加载档案失败：${error.message}`, true);
  }
}

boot().catch((error) => toast(`页面初始化失败：${error.message}`, true));
