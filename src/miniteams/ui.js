  /* ui.js: shared by widget.html and chat.html, inlined by the server (web._page) into each page's scope */
  const store = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const keep = (k, v) => { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) {} };
  // URL params override the stored choices without replacing them (bookmarks, screenshots).
  const q = new URLSearchParams(location.search), pref = (k) => q.get(k) ?? store(k);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  // One colour and initials rule for every avatar, rows and messages alike.
  const hue = (s) => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
  const initials = (name) => { const w = name.split(/[ ,]+/).filter((x) => /\p{L}/u.test(x));
    return ((w[0] || "?")[0] + ((w[1] || "")[0] || "")).toUpperCase(); };

  // The `mt` cookie authenticates the handshake: it survives a server restart, so a redial needs nothing else.
  const wsUrl = (path) => (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;
  // A launch URL's `#<token>` buys the cookie once; it leaves the address bar at once, spent or not.
  const signIn = (() => {
    const token = location.hash.slice(1);
    if (!token) return Promise.resolve();
    history.replaceState(null, "", location.pathname + location.search);
    return fetch("/auth", { headers: { "X-Miniteams-Token": token } }).catch(() => {});
  })();
  const SIGNED_OUT = "Not signed in: open the launch file (~/.config/miniteams/open.html by default) or web.json's URL (host, port and #token)";
  // A socket refused before it opened hides its status: a plain GET of /ws tells no cookie (403) from a server down.
  const dial = (path) => {
    const sock = new WebSocket(wsUrl(path));
    let up = false;
    sock.addEventListener("open", () => { up = true; document.getElementById("signin")?.remove(); });
    sock.addEventListener("close", () => {
      if (up) return;
      fetch("/ws").then((r) => {
        if (r.status !== 403 || document.getElementById("signin")) return;
        const hint = el("div", null, SIGNED_OUT);
        hint.id = "signin"; hint.setAttribute("role", "alert");
        document.body.prepend(hint);
      }).catch(() => {});
    });
    return sock;
  };
  // Reconnect delays: 1 s doubling up to 30 s; the page resets it once the server proves usable.
  const backoff = () => {
    let ms = 1000;
    return { next: () => { const d = ms; ms = Math.min(ms * 2, 30000); return d; }, reset: () => { ms = 1000; } };
  };

  // Board rows, the widget's list and the chat page's split list alike.
  // Parse, don't compare strings: Teams stamps 7 fraction digits, the server's mention time 3.
  const isSeen = (r) => !!r.seen_at && Date.parse(r.seen_at) >= Date.parse(r.last_activity);
  const mentioned = (r) => !!r.mention;  // the server clears it on seen / Teams read
  const kind = (id) => id.includes("@unq.gbl") ? "dm" : id.includes("meeting_") ? "meeting" : "group";
  const rowTitle = (r) => r.label.replace(/ · \d+p$/, "");
  const ago = (iso) => {
    const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (isNaN(s)) return "";
    return s < 60 ? "now" : s < 3600 ? Math.floor(s / 60) + "m" : s < 86400 ? Math.floor(s / 3600) + "h" : Math.floor(s / 86400) + "d";
  };
  // Case- and accent-blind text, with a folded→original index map: "seb" must reach "Sébastien",
  // and what gets highlighted is still the original spelling.
  const foldText = (str) => {
    let out = "";
    const map = [];
    for (let i = 0; i < str.length; i++) {
      const c = str[i].normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
      out += c;
      for (let k = 0; k < c.length; k++) map.push(i);
    }
    map.push(str.length);
    return [out, map];
  };
  // A muted chat stays out of Inbox / Unread / Seen whatever lands on it; a mention still surfaces.
  const filters = {
    all: (r) => mentioned(r) || (!r.muted && !isSeen(r)),
    mention: mentioned,
    unread: (r) => r.unread && !r.muted,
    seen: (r) => isSeen(r) && !r.muted,
    muted: (r) => r.muted,
  };
  // The quick filter (`query`, folded) narrows every tab, counts included: a badge must match the list.
  const named = (r, query) => !query || foldText(rowTitle(r))[0].includes(query);
  // Every occurrence, as text nodes: a chat name is data, never markup.
  const marked = (node, text, query) => {
    const [hay, map] = foldText(text);
    let at = 0, cut = 0, i;  // `at` walks the folded text, `cut` the original one
    while (query && (i = hay.indexOf(query, at)) >= 0) {
      node.append(text.slice(cut, map[i]), el("mark", null, text.slice(map[i], map[i + query.length])));
      at = i + query.length;
      cut = map[at];
    }
    node.append(text.slice(cut));
    return node;
  };
  // The mentioning text carries my name (`me`) inline (Teams strips the <at> tag): highlight it once.
  // Either form is mentioned in practice, so the full name is tried before the first name alone.
  const mentionLine = (r, into, me) => {
    const m = r.mention;
    if (m.by) into.append(el("b", null, m.by + ": "));
    const text = m.text || "", full = (me || "").trim(), [hay, map] = foldText(text);
    let from = -1, to = -1;
    for (const cand of m.kind === "me" && full ? [full, full.split(" ")[0]] : []) {
      const [needle] = foldText(cand), j = hay.indexOf(needle);
      if (j >= 0) { from = map[j]; to = map[j + needle.length]; break; }
    }
    if (from < 0) { into.append(el("span", "at", m.kind === "all" ? "@everyone" : "@you"), " " + text); return into; }
    into.append(text.slice(0, from), el("span", "at", "@" + text.slice(from, to)), text.slice(to));
    return into;
  };
  // A row's last line, as text nodes in its own direction: a snippet is data, never markup.
  const preview = (r, into, me) => {
    into.dir = "auto";
    if (r.typing && r.typing.length) { into.classList.add("typing"); into.textContent = "✍ " + r.typing.join(", "); return into; }
    if (mentioned(r)) return mentionLine(r, into, me);
    if (r.sender) into.append(el("b", null, r.sender + ": "));
    into.append(r.text || "");
    return into;
  };

  // Row buttons are display:none until the row is hovered or holds the focus: shown for the focus() call only.
  const focusIn = (li, k) => { if (!li) return; li.classList.add("hold"); li.querySelector(`[data-k="${k}"]`)?.focus(); li.classList.remove("hold"); };
  // A row's preview line opens the chat as its title does: the same click, modifiers and button, on the title.
  // A synthetic middle click has no default action: the title's native new tab is done here when nothing took it.
  const likeTitle = (prev, name) => {
    prev.onclick = prev.onauxclick = (ev) => {
      ev.preventDefault();
      if (name.dispatchEvent(new MouseEvent(ev.type, ev)) && ev.type === "auxclick" && ev.button === 1) window.open(name.href, "_blank", "noopener");
    };
    return prev;
  };

  // A row's actions: 👁 (Teams' read, seen on every device) inline, the private seen and the mute behind ⋯.
  // Rows are rebuilt on every broadcast: the open menu is kept by row id and comes back with its row.
  // Returns the inline buttons and the menu (none or one) to append to the row's `li`.
  const rowActions = (list, render, send) => {
    let open = "", up = false;
    const close = (id, restore = true) => {
      // Pointer dismissal must not leave focus pinning the row's hover actions open.
      if (!restore && list.contains(document.activeElement)) document.activeElement.blur();
      open = ""; render();
      if (restore) focusIn(list.querySelector(`li[data-id="${CSS.escape(id)}"]`), "more");
    };
    addEventListener("click", (e) => { if (open && !e.target.closest?.(".rowmenu, [data-k=more]")) { open = ""; render(); } });
    // After the move, not during it: a broadcast re-render puts the focus back into the menu first. Its own ⋯ keeps it, to toggle.
    // Only the menu node goes: a full render between press and release would drop the click that moved the focus.
    list.addEventListener("focusout", () => setTimeout(() => {
      const menu = list.querySelector(".rowmenu");
      if (!open || !menu || menu.parentElement.contains(document.activeElement)) return;
      open = "";
      menu.parentElement.querySelector("[data-k=more]")?.setAttribute("aria-expanded", "false");
      menu.remove();
    }));
    const btn = (cls, text, k, act) => { const b = el("button", cls, text); b.dataset.k = k; b.onclick = act; return b; };
    // Anchor to the trigger, not the row: previews and mention badges make rows taller.
    const anchor = (menu) => {
      const li = menu.parentElement;
      if (!li) return;
      const row = li.getBoundingClientRect(), more = li.querySelector("[data-k=more]").getBoundingClientRect();
      menu.style.right = `${row.right - more.right}px`;
      menu.style.setProperty("--menu-top", `${more.bottom - row.top + 4}px`);
      menu.style.setProperty("--menu-bottom", `${row.bottom - more.top + 4}px`);
    };
    return (r) => {
      const inline = [];
      if (r.last_id) inline.push(Object.assign(btn("read", "👁", "read", () => send({ read: r.id })), { title: "mark read in Teams" }));
      const more = btn("more", "⋯", "more", (e) => {
        if (open === r.id) { close(r.id, e.detail === 0); return; }
        open = r.id; up = false; render();
        const menu = list.querySelector(".rowmenu");
        if (menu) {
          anchor(menu);
          // Rows near the bottom of what scrolls (the list, or else the window under its sticky header) open upwards when that has more room.
          const box = getComputedStyle(list).overflowY === "visible"
            ? { top: document.querySelector("header")?.getBoundingClientRect().bottom ?? 0, bottom: innerHeight } : list.getBoundingClientRect();
          const row = menu.parentElement.querySelector("[data-k=more]").getBoundingClientRect(), bottom = Math.min(innerHeight, box.bottom);
          if (menu.getBoundingClientRect().bottom > bottom && row.top - Math.max(0, box.top) > bottom - row.bottom) { up = true; menu.classList.add("up"); }
          menu.querySelector("button")?.focus();
        }
      });
      more.title = "more"; more.setAttribute("aria-label", "more"); more.setAttribute("aria-expanded", String(open === r.id));
      inline.push(more);
      if (open !== r.id) return [inline, []];
      const m = mentioned(r), undo = isSeen(r) && !m;
      // What was on screen: a message or mention landing between render and click must keep the row visible.
      const shownAt = m && Date.parse(r.mention.at) > Date.parse(r.last_activity) ? r.mention.at : r.last_activity;
      const act = (verb) => (e) => { close(r.id, e.detail === 0); send(verb); };
      const menu = el("div", up ? "rowmenu up" : "rowmenu");
      queueMicrotask(() => anchor(menu));
      menu.append(
        btn(null, undo ? "↺ Unseen" : "✓ Mark seen", "seen", act(undo ? { unseen: r.id } : { seen: r.id, at: shownAt })),
        Object.assign(btn(null, r.muted ? "🔔 Unmute" : "🔕 Mute", "mute", act(r.muted ? { unmute: r.id } : { mute: r.id })),
          { title: r.muted ? "" : "hide until unmuted" }));
      menu.onkeydown = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(r.id); } };
      return [inline, [menu]];
    };
  };

  // `:` / `@` completion (spec 012) on `input`, its list in `pop`. The pages differ only in the hooks:
  // `chat()` the chat the text goes to, `put(text, from, to)` the insertion, `edited()` after it, and
  // `above`, the list above the caret line (composer at the window's bottom) rather than under it.
  // The server answers {complete, q, items}; only the answer to the latest question counts, an older
  // one arriving late would list the wrong names.
  const completion = ({ input, pop, send, chat, put, edited, above }) => {
    const POP_ASK_MS = 120;  // typing pause before a question
    const POP_HIDE_MS = 2000;  // an unanswered question drops the list: Enter must not wait on it forever
    for (const [k, v] of [["role", "combobox"], ["aria-autocomplete", "list"], ["aria-controls", pop.id], ["aria-expanded", "false"]]) input.setAttribute(k, v);
    pop.setAttribute("role", "listbox");
    pop.onmousedown = (e) => e.preventDefault();  // its scrollbar too: a press there must not blur the input
    let comp = null, want = null, timer = 0, dismissed = -1;
    const picked = new Map();  // chat id → ordered picks: repeated first names can have different recipients
    const token = () => {
      const at = input.selectionStart;
      if (at !== input.selectionEnd) return null;
      // Word start only: `10:30` and `me@host` must not ask anything. One inner space reaches a last name
      // (`@ann l`); an inserted `@Ann Lee ` ends in a second one, so it never asks again.
      // Emoji first: the member pattern's inner space would let `@ann :fi` read as one name.
      const before = input.value.slice(0, at), e = /(?:^|\s):([a-z0-9_+-]{2,})$/i.exec(before);
      const m = e || /(?:^|\s)@([^\s@]*(?: [^\s@]*)?)$/.exec(before);
      return m && { kind: e ? "emoji" : "member", q: m[1], start: at - m[1].length - 1 };
    };
    // A textarea exposes no caret coordinates: lay the text before the caret out in a hidden copy.
    const caret = () => {
      const cs = getComputedStyle(input), copy = el("div", null, input.value.slice(0, input.selectionStart)), mark = el("span", null, "​");
      for (const k of ["font", "padding", "border", "boxSizing", "lineHeight", "letterSpacing", "tabSize", "width"]) copy.style[k] = cs[k];
      Object.assign(copy.style, { position: "absolute", visibility: "hidden", whiteSpace: "pre-wrap", overflowWrap: "break-word", top: 0, left: 0 });
      copy.append(mark); document.body.append(copy);
      const xy = [mark.offsetLeft, mark.offsetTop, mark.offsetHeight];
      copy.remove();
      return xy;
    };
    const render = () => {
      pop.hidden = !comp;
      input.setAttribute("aria-expanded", String(!!comp));
      if (!comp) { pop.replaceChildren(); input.removeAttribute("aria-activedescendant"); return; }
      pop.setAttribute("aria-label", comp.kind === "emoji" ? "emoji" : "members");
      pop.replaceChildren(...comp.items.map((it, i) => {
        const o = el("div");
        o.id = "pop-" + i; o.setAttribute("role", "option"); o.setAttribute("aria-selected", String(i === comp.active));
        if (comp.kind === "emoji") {
          // Images only from our own /emoji/ route: the item comes over the socket, the page trusts no URL.
          const own = it.custom && /^\/emoji\/[\w-]+$/.test(it.src || "");
          o.append(own ? Object.assign(el("img"), { src: it.src, alt: "" }) : el("span", "ch", it.custom ? "" : it.char));
        }
        o.append(Object.assign(el("span", "truncate", comp.kind === "emoji" ? `:${it.name}:` : it.name), { dir: "auto" }));
        o.onclick = () => { const t = word(); if (t && t.q === comp.q) { comp.active = i; insert(t); } };
        return o;
      }));
      input.setAttribute("aria-activedescendant", "pop-" + comp.active);
      const [x, y, h] = caret();
      pop.style.top = input.offsetTop - input.scrollTop + (above ? y - pop.offsetHeight : y + h) + "px";
      pop.style.left = input.offsetLeft + Math.max(0, Math.min(x, input.offsetWidth - pop.offsetWidth)) + "px";
      document.getElementById("pop-" + comp.active).scrollIntoView({ block: "nearest" });
    };
    const close = () => { clearTimeout(timer); want = null; comp = null; render(); };
    const lookup = () => {
      clearTimeout(timer); want = null;
      const t = token();
      if (!t || t.start === dismissed) { if (!t) dismissed = -1; comp = null; render(); return; }
      timer = setTimeout(() => {
        // No socket or no answer: drop the list, or Enter would wait forever on a prefix nobody answers.
        if (!send({ complete: t.kind, q: t.q, thread: chat() })) { close(); return; }
        want = t;
        timer = setTimeout(close, POP_HIDE_MS);
      }, POP_ASK_MS);
    };
    const answered = (msg) => {
      if (!want || msg.complete !== want.kind || msg.q !== want.q) return;
      clearTimeout(timer);
      // A malformed item would throw mid-render and leave the list stuck open.
      const items = Array.isArray(msg.items) && !msg.error ? msg.items.filter((it) => it && typeof it.name === "string") : [];
      comp = items.length ? { ...want, items, active: 0 } : null;
      render();
    };
    // The word the open list answers, or null once the caret left it.
    const word = () => { const t = token(); return t && t.start === comp.start && t.kind === comp.kind ? t : null; };
    const pick = (m) => ({ name: m.name, mri: m.mri, ...(m.everyone === true ? { everyone: true } : {}) });
    // The server's mention rule (compose.py): `@` starts a word, the name ends one (Python's Unicode \w),
    // longest names first. The names of the `@Name`s it would read, in text order.
    const scan = (text, names) => {
      const alt = [...new Set(names.filter(Boolean))].sort((a, b) => [...b].length - [...a].length)
        .map((n) => n.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")).join("|");
      return alt ? [...text.matchAll(new RegExp(`(?<![\\p{L}\\p{N}_])@(${alt})(?![\\p{L}\\p{N}_])`, "gu"))].map((m) => m[1]) : [];
    };
    const insert = (t) => {
      const it = comp.items[comp.active], member = comp.kind === "member", text = member ? `@${it.name} ` : it.custom ? `:${it.name}:` : it.char;
      if (member) {
        const id = chat();
        picked.set(id, [...(picked.get(id) || []), pick(it)]);
      }
      close();
      if (member) dismissed = t.start;  // `@everyone ` + the next word must not ask `everyone t`…
      put(text, t.start, input.selectionStart);
      if (member) picked.set(chat(), mentions(input.value));
      edited();
    };
    // Enter with the list open picks, never sends; true when the key was the list's. While a longer
    // prefix is still being asked, the list on screen answers a shorter one: Enter/Tab wait for it.
    const key = (e) => {
      if (e.isComposing || e.keyCode === 229) return false;
      if (e.key === "Backspace" && !e.ctrlKey && !e.altKey && !e.metaKey && input.selectionStart === input.selectionEnd) {
        const at = input.selectionStart, before = input.value.slice(0, at);
        for (const m of [...(picked.get(chat()) || [])].reverse()) {
          const first = m.name.split(/\s+/)[0];
          if (m.everyone || first === m.name) continue;
          const space = before.endsWith(`@${m.name} `) ? " " : "", start = at - m.name.length - 1 - space.length;
          // Both ends on a word boundary, as `scan` reads a mention: `@Ann Leex` is no `@Ann Lee`.
          if (!before.endsWith(`@${m.name}${space}`) || /[\p{L}\p{N}_]$/u.test(before.slice(0, start))
            || !space && /^[\p{L}\p{N}_]/u.test(input.value.slice(at))) continue;
          // Resolve occurrences before changing the label: namesakes must keep their own recipient.
          const active = mentions(input.value), names = active.map((p) => p.name), seen = new Map();
          const ordered = scan(input.value, names).map((name) => {
            const matches = active.filter((p) => p.name === name), nth = seen.get(name) || 0;
            seen.set(name, nth + 1);
            return matches[Math.min(nth, matches.length - 1)];
          });
          // Inside a longer picked name (`@Ann Lee` of `@Ann Lee Smith`) the server reads that one.
          if (scan(input.value.slice(start), names)[0] !== m.name) continue;
          const pos = scan(input.value.slice(0, start), names).length;
          ordered[pos] = pick({ ...ordered[pos], name: first });
          picked.set(chat(), ordered);
          close(); dismissed = start;
          put(`@${first}${space}`, start, at); edited();
          return true;
        }
      }
      if (!comp) return false;
      const n = comp.items.length;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { comp.active = (comp.active + (e.key === "ArrowDown" ? 1 : n - 1)) % n; render(); }
      else if ((e.key === "Enter" && !e.shiftKey) || e.key === "Tab") {
        const t = word();
        if (!t) { close(); return false; }  // caret left the word: the key is the input's again
        if (t.q === comp.q) insert(t);
      }
      else if (e.key === "Escape") { dismissed = comp.start; close(); }
      else return false;
      return true;
    };
    // Another chat or a reopened composer: a word dismissed before may be asked again.
    const reset = () => { dismissed = -1; close(); };
    // Names, not offsets: of the picks under one name the last `count` stand, `count` its `@Name`s in the
    // text; a pick deleted from the text then re-picked as a namesake must not take the n-th `@Name`.
    const mentions = (text, id = chat()) => {
      const all = picked.get(id) || [], count = new Map();
      for (const name of scan(text, all.map((m) => m.name))) count.set(name, (count.get(name) || 0) + 1);
      return all.filter((m, i) => all.slice(i + 1).filter((n) => n.name === m.name).length < (count.get(m.name) || 0));
    };
    // Picks saved across a reload: data from sessionStorage, checked like any input.
    const restore = (list) => {
      const ok = list.filter((m) => m && typeof m.name === "string" && typeof m.mri === "string" && [undefined, true].includes(m.everyone));
      picked.set(chat(), ok.map(pick));
    };
    // A sent text empties the composer without an input event: its dismissed word must not outlive it.
    const forget = (id) => { picked.delete(id); if (id === chat()) dismissed = -1; };
    input.addEventListener("input", (e) => { if (!e.isComposing) lookup(); });
    input.addEventListener("compositionend", lookup);
    input.addEventListener("blur", close);
    return { mentions, restore, forget, answered, key, close, reset };
  };
