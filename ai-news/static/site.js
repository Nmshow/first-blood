(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const state = { items: [], filter: "全部" };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
  }

  function formatDate(value, options = {}) {
    if (!value) return "日期待核实";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "日期待核实";
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      ...(options.time ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}),
    }).formatToParts(date).reduce((result, part) => {
      if (part.type !== "literal") result[part.type] = part.value;
      return result;
    }, {});
    const day = `${parts.year}年${parts.month}月${parts.day}日`;
    return options.time ? `${day} ${parts.hour}:${parts.minute}` : day;
  }

  function compactDate(value) {
    if (!value) return ["日期", "待核实"];
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return ["日期", "待核实"];
    const parts = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit" }).formatToParts(date).reduce((result, part) => {
      if (part.type !== "literal") result[part.type] = part.value;
      return result;
    }, {});
    return [parts.month || "", parts.day || ""];
  }

  function storyCard(item, index) {
    const [month, day] = compactDate(item.published_at);
    const url = /^https?:\/\//i.test(item.source_url || item.canonical_url || "") ? (item.source_url || item.canonical_url) : "#";
    const title = escapeHtml(item.title);
    return `<article class="story-card" style="animation-delay:${Math.min(index * 35, 280)}ms">
      <time class="story-date" datetime="${escapeHtml(item.published_at || "")}"><strong>${escapeHtml(month)}月${escapeHtml(day)}日</strong>${escapeHtml(item.publisher || item.source_name || "官方来源")}</time>
      <div class="story-content">
        <div class="story-meta"><span class="story-publisher">${escapeHtml(item.publisher || item.source_name || "官方来源")}</span><span class="story-meta-dot"></span><span>${escapeHtml(item.channel || "官方公开渠道")}</span><span class="story-category">${escapeHtml(item.category || "综合")}</span><span class="verified-mark">${escapeHtml(item.verified_by || "官方原文已核验")}</span></div>
        <h3 class="story-title"><a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${title}</a></h3>
        <p class="story-background">${escapeHtml(item.background || "背景整理待补充。")}</p>
        <p class="story-summary">${escapeHtml(item.summary || "中文摘要待处理。")}</p>
        <div class="story-footer"><span class="story-proof">${escapeHtml(item.evidence_note || "可追溯至官方原文")}</span><a class="story-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">查看官方原文 <span aria-hidden="true">↗</span></a></div>
      </div>
    </article>`;
  }

  function renderStories() {
    const groups = {
      "安全": ["模型安全", "安全治理"],
      "产品": ["智能办公", "算力基础设施"],
      "研究": ["隐私与安全"],
      "商业": ["企业数据", "教育应用", "公司战略"],
    };
    const visible = state.filter === "全部" ? state.items : state.items.filter((item) => (groups[state.filter] || [state.filter]).includes(item.category));
    byId("story-list").innerHTML = visible.map(storyCard).join("");
    byId("empty-state").hidden = visible.length !== 0;
    byId("shortage-note").hidden = state.filter !== "全部" || state.items.length >= 5;
    if (!visible.length) byId("story-list").innerHTML = "";
    byId("featured-count").textContent = String(state.items.length).padStart(2, "0");
  }

  function sourceStatusClass(status) {
    return ["采集正常", "已接入"].includes(status) ? "good" : "pending";
  }

  function renderSources(sources) {
    const list = byId("source-list");
    byId("source-total").textContent = `${sources.length} 个渠道`;
    list.innerHTML = sources.map((source) => `<li class="source-row">
      <span class="source-name">${escapeHtml(source.name)}</span>
      <span class="source-state ${sourceStatusClass(source.status)}">${escapeHtml(source.status || "待探测")}</span>
    </li>`).join("");
  }

  function setUpdateText(news, status) {
    const last = news.last_collection_at || status.last_collection_at;
    const configured = Boolean(news.translation_configured);
    byId("today").textContent = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "long", day: "numeric", weekday: "long" }).format(new Date());
    byId("update-state").textContent = last ? `最近检查：${formatDate(last, { time: true })}` : "已整理近期官方公开资讯";
    byId("last-update").textContent = last ? `最近检查 ${formatDate(last, { time: true })} · 每 ${news.collection_interval_minutes || 30} 分钟` : `每 ${news.collection_interval_minutes || 30} 分钟自动检查一次公开来源`;
    byId("ops-dot").classList.toggle("running", Boolean(status.pending_count));
    if (!configured) byId("refresh-message").textContent = "英文新稿需配置中文摘要服务后发布";
  }

  async function loadPage() {
    try {
      const [newsResponse, statusResponse] = await Promise.all([fetch("/api/news", { cache: "no-store" }), fetch("/api/status", { cache: "no-store" })]);
      if (!newsResponse.ok || !statusResponse.ok) throw new Error("接口暂时不可用");
      const news = await newsResponse.json();
      const status = await statusResponse.json();
      state.items = news.items || [];
      renderStories();
      renderSources(status.sources || []);
      setUpdateText(news, status);
    } catch (error) {
      byId("story-list").innerHTML = "";
      byId("empty-state").hidden = false;
      byId("empty-state").querySelector("h3").textContent = "暂时无法读取资讯";
      byId("empty-state").querySelector("p").textContent = "请确认本地采集服务正在运行，然后重新加载页面。";
      byId("update-state").textContent = "暂时无法连接采集服务";
      byId("refresh-message").textContent = error.message;
    }
  }

  document.querySelectorAll(".filter").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".filter").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.filter = button.dataset.filter || "全部";
      renderStories();
    });
  });

  byId("show-method").addEventListener("click", (event) => {
    event.preventDefault();
    const details = byId("核验详情");
    details.hidden = !details.hidden;
    byId("show-method").innerHTML = details.hidden ? '查看核验说明 <span aria-hidden="true">↗</span>' : '收起核验说明 <span aria-hidden="true">↖</span>';
  });

  byId("refresh-button").addEventListener("click", async () => {
    const button = byId("refresh-button");
    button.disabled = true;
    byId("ops-dot").classList.add("running");
    byId("refresh-message").textContent = "正在逐个检查已启用的公开来源…";
    try {
      const response = await fetch("/api/collect", { method: "POST" });
      const result = await response.json();
      byId("refresh-message").textContent = result.message || "采集任务已开始。完成后页面会更新来源状态。";
      window.setTimeout(loadPage, 2500);
    } catch (_) {
      byId("refresh-message").textContent = "无法连接本地采集服务。";
    } finally {
      window.setTimeout(() => { button.disabled = false; byId("ops-dot").classList.remove("running"); }, 1800);
    }
  });

  loadPage();
  window.setInterval(loadPage, 60_000);
})();
