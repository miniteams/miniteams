  /* ui.js: shared by widget.html and chat.html, inlined by the server (web._page) into each page's scope */
  const store = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const keep = (k, v) => { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) {} };
  // URL params override the stored choices without replacing them (bookmarks, screenshots).
  const q = new URLSearchParams(location.search), pref = (k) => q.get(k) ?? store(k);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  // A /media image not served yet (archiver a moment behind, Teams slow) is asked again for about 30 s,
  // then said. Delegated, in capture: `error` does not bubble, and the CSP forbids inline handlers.
  // A Giphy GIF (spec 031) then tries Giphy itself, then reads as the link to it; one the web fetched
  // live (`?giphy=`) goes there at once: the web's failure memo outlives the retries.
  const MEDIA_RETRY_MS = [1000, 3000, 7000, 19000];
  const giphyLink = (img) => {
    const alt = img.alt.trim(), text = alt ? `[image: ${alt}]` : "[image not archived]";
    if (img.closest("a")) return text;  // an anchor in an anchor would split the outer one
    const a = el("a", null, text);
    Object.assign(a, { href: img.dataset.giphy, rel: "noopener", target: "_blank" });
    return a;
  };
  document.addEventListener("error", (e) => {
    const img = e.target;
    if (!(img instanceof HTMLImageElement)) return;
    const src = img.getAttribute("src") || "", giphy = img.dataset.giphy;
    if (giphy && src === giphy) { img.replaceWith(giphyLink(img)); return; }
    if (!src.startsWith("/media/")) return;
    const n = Number(img.dataset.retry || 0);
    if (n >= MEDIA_RETRY_MS.length || (giphy && src.includes("?giphy="))) {
      if (!giphy) { img.replaceWith("[image not archived]"); return; }
      img.src = giphy;
      return;
    }
    img.dataset.retry = n + 1;
    setTimeout(() => { img.src = img.getAttribute("src"); }, MEDIA_RETRY_MS[n]);
  }, true);
  // One colour and initials rule for every avatar, rows and messages alike.
  const hue = (s) => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
  const initials = (name) => { const w = name.split(/[ ,]+/).filter((x) => /\p{L}/u.test(x));
    return ((w[0] || "?")[0] + ((w[1] || "")[0] || "")).toUpperCase(); };

  // Presence (spec 015): mri → {availability, activity, confirmed} from the server's `presence` frames.
  // Anyone missing is unknown: no dot, never Offline. An avatar opts in with `data-pmri`.
  let presence = new Map();
  const PRESENCE = { Available: "ok", AvailableIdle: "ok", Busy: "busy", BusyIdle: "busy", DoNotDisturb: "dnd",
    Away: "away", BeRightBack: "away", Offline: "off" };
  const words = (s) => s.replace(/(?<=.)(?=[A-Z])/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());
  const presenceLabel = (p) => {
    const a = words(p.availability), x = words(p.activity);
    return (x && x !== a ? `${a} · ${x}` : a) + (p.confirmed ? "" : " (unconfirmed)");
  };
  const presenceDot = (av) => {
    av.querySelector(":scope > .pres")?.remove();
    av.removeAttribute("title");
    const p = presence.get(av.dataset.pmri), cls = p && typeof p.availability === "string" && Object.hasOwn(PRESENCE, p.availability) && PRESENCE[p.availability];
    if (!cls) return av;
    const dot = el("span", "pres " + cls + (p.confirmed ? "" : " unsure"));
    av.title = presenceLabel({ ...p, activity: typeof p.activity === "string" ? p.activity : "" });
    dot.setAttribute("role", "img"); dot.setAttribute("aria-label", av.title);
    av.append(dot);
    return av;
  };
  // Every avatar of a person changes at once, without rebuilding the lists.
  const paintPresence = (map) => {
    presence = new Map(map && typeof map === "object" && !Array.isArray(map) ? Object.entries(map) : []);
    document.querySelectorAll("[data-pmri]").forEach(presenceDot);
  };
  // Spec 036: my own status in the header (`box`), its menu forcing a Teams status through `web`. The dot only
  // follows presence frames: a pick changes it when the service's callback says so, never before.
  const OWN = [["Available", "Available", "ok"], ["Busy", "Busy", "busy"], ["DoNotDisturb", "Do not disturb", "dnd"],
    ["BeRightBack", "Be right back", "away"], ["Away", "Appear away", "away"], ["AppearOffline", "Appear offline", "off"]];
  const OWN_ERROR_MS = 10000;
  const ownStatus = (box, send) => {
    let me = "", online = false, pending = false, menu = null, errTimer = 0;
    const btn = el("button", "ownbtn"), dot = el("span"), err = el("div", "ownerr");
    btn.id = "mystatus";
    btn.setAttribute("aria-haspopup", "menu"); btn.setAttribute("aria-expanded", "false");
    err.setAttribute("role", "status");  // emptied, never hidden: a live region must stay in the tree
    btn.append(dot); box.append(btn); document.body.append(err);
    const mine = () => { const p = presence.get(me); return p && typeof p.availability === "string" && Object.hasOwn(PRESENCE, p.availability) ? p : null; };
    // Fixed under the button, inside the viewport: the widget header's backdrop-filter would capture a
    // fixed child, so both boxes live in the body.
    const under = (node) => {
      node.style.left = "0px";  // measured from the left edge: a box placed further right is squeezed to wrap
      const r = btn.getBoundingClientRect(), b = node.getBoundingClientRect(), w = document.documentElement.clientWidth, h = document.documentElement.clientHeight;
      Object.assign(node.style, { left: `${Math.max(0, Math.min(r.right - b.width, w - b.width))}px`, top: `${Math.max(0, Math.min(r.bottom + 4, h - b.height))}px` });
    };
    const draw = () => {
      const p = mine(), text = p ? presenceLabel({ ...p, activity: typeof p.activity === "string" ? p.activity : "" }) : "Status unknown";
      dot.className = "pres " + (p ? PRESENCE[p.availability] + (p.confirmed ? "" : " unsure") : "unknown");
      btn.title = text; btn.setAttribute("aria-label", "my status: " + text);
      if (!menu) return;
      menu.querySelector(".cur").textContent = text;
      for (const b of menu.querySelectorAll("button")) {
        if (b.getAttribute("role") === "menuitemradio") b.setAttribute("aria-checked", String(!!p && p.forced === b.dataset.v));
        b.setAttribute("aria-disabled", String(!online || pending));
      }
      under(menu);  // a new label resizes it: keep it on the button, inside the viewport
    };
    const close = (back) => {
      if (!menu) return;
      const m = menu;
      menu = null;  // first: Chrome's focusout on removal must find it closed
      m.remove();
      btn.setAttribute("aria-expanded", "false");
      if (back) btn.focus();
    };
    const pick = (v) => {
      if (!online || pending || !send({ setpresence: v })) return;
      pending = true;
      close(true);
    };
    const item = (v, text, cls, role) => {
      const b = el("button");
      b.dataset.v = v; b.tabIndex = -1; b.setAttribute("role", role);
      if (cls) b.append(el("span", "pres " + cls));
      b.append(text);
      b.onclick = () => pick(v);
      return b;
    };
    const open = (last) => {
      menu = el("div", "rowmenu ownmenu");
      menu.tabIndex = -1;  // a click on its padding keeps the focus, and so the menu, in it
      menu.setAttribute("role", "menu"); menu.setAttribute("aria-label", "my status");
      const sep = el("div", "sep");
      sep.setAttribute("role", "separator");
      menu.append(el("div", "cur"), ...OWN.map(([v, text, cls]) => item(v, text, cls, "menuitemradio")), sep, item("Reset", "Reset status", null, "menuitem"));
      menu.onkeydown = (e) => {
        const all = [...menu.querySelectorAll("button")], n = all.length, i = all.indexOf(document.activeElement);
        const to = { ArrowDown: (i + 1) % n, ArrowUp: i < 1 ? n - 1 : i - 1, Home: 0, End: n - 1 }[e.key];
        if (to !== undefined) all[to].focus();
        else if (e.key === "Escape") close(true);
        else if (e.key === "Tab") { close(true); return; }  // the Tab goes on from the button
        else return;
        e.preventDefault(); e.stopPropagation();
      };
      menu.addEventListener("focusout", (e) => { if (e.currentTarget === menu && !menu.contains(e.relatedTarget) && e.relatedTarget !== btn) close(false); });
      document.body.append(menu);
      btn.setAttribute("aria-expanded", "true");
      draw();
      const all = [...menu.querySelectorAll("button")];
      (last ? all.at(-1) : menu.querySelector("[aria-checked=true]") || all[0]).focus({ preventScroll: true });
    };
    btn.onclick = () => { if (menu) close(true); else open(false); };
    btn.onkeydown = (e) => {
      if ((e.key !== "ArrowDown" && e.key !== "ArrowUp") || menu) return;
      e.preventDefault(); open(e.key === "ArrowUp");
    };
    // Capture: a press elsewhere closes it even where the target keeps the focus from moving.
    addEventListener("pointerdown", (e) => { if (menu && !menu.contains(e.target) && !btn.contains(e.target)) close(false); }, true);
    addEventListener("resize", () => { if (menu) under(menu); if (err.textContent) under(err); });
    draw();
    return {
      paint: (self) => { me = typeof self === "string" ? self : ""; draw(); },
      opened: () => { online = true; draw(); },
      closed: () => { online = false; pending = false; draw(); },  // that socket's answer will never come
      answered: (m) => {
        pending = false;
        clearTimeout(errTimer);
        err.textContent = m.ok ? "" : "status not changed: " + (typeof m.error === "string" && m.error ? m.error : "failed");
        if (err.textContent) { under(err); errTimer = setTimeout(() => { err.textContent = ""; }, OWN_ERROR_MS); }
        draw();
      },
    };
  };

  // The `mt` cookie authenticates the handshake: it survives a server restart, so a redial needs nothing else.
  const wsUrl = (path) => (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;
  // A launch URL's `#<token>` buys the cookie once; it leaves the address bar at once, spent or not.
  const signIn = (() => {
    const token = location.hash.slice(1);
    if (!token) return Promise.resolve();
    history.replaceState(null, "", location.pathname + location.search);
    return fetch("/auth", { headers: { "X-Miniteams-Token": token } }).catch(() => {});
  })();
  const SIGNED_OUT = "Not signed in: open http://host:port/#token from web.json (~/.config/miniteams/web.json by default)";
  const SIGNED_OUT_NATIVE = "Not signed in: restart `miniteams widget` (it signs in with web.json's token)";
  // A socket refused before it opened hides its status: a plain GET of /ws tells no cookie (403) from a server down.
  const dial = (path) => {
    const sock = new WebSocket(wsUrl(path));
    let up = false;
    sock.addEventListener("open", () => { up = true; document.getElementById("signin")?.remove(); });
    sock.addEventListener("close", () => {
      if (up) return;
      fetch("/ws").then((r) => {
        if (r.status !== 403 || document.getElementById("signin")) return;
        const hint = el("div", null, window.miniteamsTray ? SIGNED_OUT_NATIVE : SIGNED_OUT);  // the native window (spec 024)
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
  // Spec 036: the worst connection problem, under the header. Any new problem shows once it has lasted 2 s,
  // so a reload or a quick server restart does not flash it; recovery hides it at once.
  const BANNER_GRACE_MS = 2000;
  const BANNER = {
    page: () => ["Disconnected from miniteams, reconnecting…"],
    login: () => ["Teams sign-in expired: run ", el("code", null, "miniteams login")],
    down: () => ["Teams connection lost, reconnecting…"],
  };
  // Hidden by emptying, never `hidden`: a live region must stay in the tree for its new text to be announced.
  const linkBanner = (box) => {
    let open = false, link = null, timer = null, shown = null;  // link: the server's last word on this socket
    // "unknown": reopened, the server's link frame not in yet; it keeps whatever shows.
    const problem = () => !open ? "page" : link === null ? "unknown"
      : link === "login" || link === "down" ? link : null;
    const show = (p) => { if (p !== shown) { shown = p; box.replaceChildren(...(p ? BANNER[p]() : [])); } };
    const update = () => {
      const p = problem();
      if (p === "unknown") return;
      if (!p || p === shown) { clearTimeout(timer); timer = null; show(p); return; }
      timer ??= setTimeout(() => {
        timer = null;
        const q = problem();
        if (q && q !== "unknown") show(q);
      }, BANNER_GRACE_MS);
    };
    update();
    return {
      opened: () => { open = true; update(); },
      closed: () => { open = false; link = null; update(); },  // while away the Teams link is unknown
      link: (state) => { link = state; update(); },
    };
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

  // A list item is a chat row (`data-id`) or a section header (`data-sid`, "" for Other, spec 026); its
  // key is the row id, or "§" and the section id. Rows are rebuilt on every broadcast: keys find them again.
  const itemKey = (li) => li?.dataset.id ?? (li?.dataset.sid !== undefined ? "§" + li.dataset.sid : undefined);
  const itemOf = (list, key) => key ? list.querySelector(key[0] === "§" ? `li[data-sid="${CSS.escape(key.slice(1))}"]` : `li[data-id="${CSS.escape(key)}"]`) : null;

  // A row's actions: 👁 (Teams' read, seen on every device) inline, the private seen, the mute and the
  // section behind ⋯; a section header's ⋯ manages the section (spec 026). One menu open at a time, kept
  // by item key across re-renders. `secs`: the page's sectionList. Returns the inline buttons and the menu
  // (none or one) to append to the item's `li`.
  const rowActions = (list, render, send, secs) => {
    let open = "", up = false, sub = "", typed = null, composing = false;  // sub: the menu's inner page; typed: its name input
    const shut = () => { open = ""; sub = ""; typed = null; composing = false; };
    const close = (key, restore = true) => {
      // Pointer dismissal must not leave focus pinning the row's hover actions open.
      if (!restore && list.contains(document.activeElement)) document.activeElement.blur();
      shut(); render();
      if (restore) focusIn(itemOf(list, key), "more");
    };
    addEventListener("click", (e) => { if (open && !e.target.closest?.(".rowmenu, [data-k=more]")) { shut(); render(); } });
    // After the move, not during it: a broadcast re-render puts the focus back into the menu first. Its own ⋯ keeps it, to toggle.
    // Only the menu node goes: a full render between press and release would drop the click that moved the focus.
    list.addEventListener("focusout", () => setTimeout(() => {
      const menu = list.querySelector(".rowmenu");
      if (!open || !menu || menu.parentElement.contains(document.activeElement)) return;
      shut();
      const li = menu.parentElement;
      li.querySelector("[data-k=more]")?.setAttribute("aria-expanded", "false");
      menu.remove();
      // The menu took its drag (sectionList); an item never draggable (Recent mode) has no attribute.
      if (li.hasAttribute("draggable") && li.dataset.sid !== "") li.draggable = true;
    }));
    const btn = (cls, text, k, act) => { const b = el("button", cls, text); b.dataset.k = k; b.onclick = act; return b; };
    const pick = (on, text, k, act) => { const b = btn(null, (on ? "✓ " : "") + text, k, act); b.setAttribute("aria-pressed", String(on)); return b; };
    // Anchor to the trigger, not the row: previews and mention badges make rows taller.
    const anchor = (menu) => {
      const li = menu.parentElement;
      if (!li) return;
      const row = li.getBoundingClientRect(), more = li.querySelector("[data-k=more]").getBoundingClientRect();
      menu.style.right = `${row.right - more.right}px`;
      menu.style.setProperty("--menu-top", `${more.bottom - row.top + 4}px`);
      menu.style.setProperty("--menu-bottom", `${row.bottom - more.top + 4}px`);
    };
    // `build(act, to)` fills the open menu: `act(verb)` sends and closes, `to(page)` turns to an inner page.
    const menuOf = (key, build) => {
      const more = btn("more", "⋯", "more", (e) => {
        if (open === key) { close(key, e.detail === 0); return; }
        shut(); open = key; up = false; render();
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
      more.title = "more"; more.setAttribute("aria-label", "more"); more.setAttribute("aria-expanded", String(open === key));
      if (open !== key) return [more, []];
      const act = (verb) => (e) => { close(key, e.detail === 0); send(verb); };
      const to = (page) => () => {
        sub = page; typed = null; composing = false; render();
        const menu = list.querySelector(".rowmenu");
        (menu?.querySelector("input") || menu?.querySelector("button"))?.focus();
      };
      // Kept across re-renders: a broadcast must not drop what is being typed. Plain text, sent as typed.
      const ask = (value, label, ok, verb) => {
        if (!typed) {
          typed = Object.assign(el("input"), { value, maxLength: 60 }); typed.dataset.k = "sname"; typed.setAttribute("aria-label", label);
          typed.addEventListener("compositionstart", () => { composing = true; });
          typed.addEventListener("compositionend", () => { composing = false; render(); });
        }
        const go = (e) => { const v = typed.value.trim(); if (v) act(verb(v))(e); };
        typed.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); go(e); } };
        return [typed, btn(null, ok, "ok", go), btn(null, "Cancel", "cancel", to(""))];
      };
      const menu = el("div", up ? "rowmenu up" : "rowmenu");
      queueMicrotask(() => anchor(menu));
      menu.append(...build(act, to, ask));
      menu.onkeydown = (e) => { if (e.key === "Escape") { e.stopPropagation(); if (sub) to("")(); else close(key); } };
      return [more, [menu]];
    };
    const row = (r) => {
      const inline = [];
      if (r.last_id) inline.push(Object.assign(btn("read", "👁", "read", () => send({ read: r.id })), { title: "mark read in Teams" }));
      const [more, menu] = menuOf(r.id, (act, to, ask) => {
        if (sub === "new") return ask("", "new section name", "Create", (name) => ({ section_new: name, chat: r.id }));
        if (sub === "file") {
          const at = secs.holder(r.id);
          return [...secs.list().map((s) => pick(s === at, s.name, "sec", act({ section_file: r.id, section: s.id }))),
            pick(!at, "None", "none", act({ section_file: r.id, section: null })), btn(null, "New section…", "new", to("new"))];
        }
        const m = mentioned(r), undo = isSeen(r) && !m;
        // What was on screen: a message or mention landing between render and click must keep the row visible.
        const shownAt = m && Date.parse(r.mention.at) > Date.parse(r.last_activity) ? r.mention.at : r.last_activity;
        return [btn(null, undo ? "↺ Unseen" : "✓ Mark seen", "seen", act(undo ? { unseen: r.id } : { seen: r.id, at: shownAt })),
          Object.assign(btn(null, r.muted ? "🔔 Unmute" : "🔕 Mute", "mute", act(r.muted ? { unmute: r.id } : { mute: r.id })),
            { title: r.muted ? "" : "hide until unmuted" }),
          btn(null, "Section ▸", "file", to("file"))];
      });
      inline.push(more);
      return [inline, menu];
    };
    const head = (s) => menuOf("§" + s.id, (act, to, ask) => {
      if (sub === "rename") return ask(s.name, "section name", "Rename", (name) => ({ section_rename: s.id, name }));
      if (sub === "delete") {
        // The focus goes on to the next item that outlives the section: one of its chats (now under Other),
        // else a header still there; else the previous one, else the filter box.
        const gone = (e) => {
          const li = itemOf(list, "§" + s.id), stays = (n) => n.dataset.id || (secs.list().length > 1 && n.dataset.sid !== undefined);
          let n = li?.nextElementSibling;
          while (n && !stays(n)) n = n.nextElementSibling;
          if (!n) { n = li?.previousElementSibling; while (n && !stays(n)) n = n.previousElementSibling; }
          act({ section_delete: s.id })(e);
          secs.land(itemKey(n));
        };
        return [el("p", null, `Delete ${s.name}? Its chats go to Other.`), btn(null, "Delete", "ok", gone), btn(null, "Cancel", "cancel", to(""))];
      }
      const all = secs.list(), i = all.findIndex((x) => x.id === s.id);
      return [btn(null, "Rename…", "rename", to("rename")),
        pick(s.order === "recent", "Recent order", "order:recent", act({ section_order: s.id, order: "recent" })),
        pick(s.order === "manual", "Manual order", "order:manual", act({ section_order: s.id, order: "manual" })),
        ...(i > 0 ? [btn(null, "↑ Move up", "up", act({ section_move: s.id, by: -1 }))] : []),
        ...(i < all.length - 1 ? [btn(null, "↓ Move down", "down", act({ section_move: s.id, by: 1 }))] : []),
        btn(null, "Delete…", "delete", to("delete"))];
    });
    // A redraw would move the name box and cancel an IME composition, or drop a drag's source: the page waits for their end.
    return Object.assign(row, { head, held: () => secs.dragging() || (composing && document.activeElement === typed) });
  };

  // The list's sections (spec 026), from the board frame only: a change shows once the server broadcasts
  // it, in every view alike. The mode and the folds are per view (`view`), on their own keys: the ⚙
  // settings are shared. `render()` redraws the page's list, `select(key)` sets its selection, `filter`
  // is its filter box.
  const sectionList = ({ view, list, send, render, select, filter }) => {
    const MODE = "sections:" + view, FOLDS = "folds:" + view;
    let sections = [], mode = store(MODE) === "sections" ? "sections" : "recent", searching = false, shownIn = new Map();  // section id → its chats on screen
    const saved = () => { try { const ids = JSON.parse(store(FOLDS)); return new Set(Array.isArray(ids) ? ids.filter((id) => typeof id === "string") : []); } catch (e) { return new Set(); } };
    const folded = saved(), searched = new Set();  // searched: folds made during a search, gone with it
    const on = () => mode === "sections" && sections.length > 0;
    const set = (all) => {
      sections = (Array.isArray(all) ? all : []).filter((s) => s && typeof s.id === "string" && s.id && typeof s.name === "string" && Array.isArray(s.chats))
        .map((s) => ({ id: s.id, name: s.name, order: s.order === "manual" ? "manual" : "recent", chats: s.chats.filter((c) => typeof c === "string") }));
    };
    const holder = (id) => sections.find((s) => s.chats.includes(id));
    const fold = (sid, shut) => {
      const into = searching ? searched : folded;
      if (shut) into.add(sid); else into.delete(sid);
      if (searching) { render(); return; }
      // Only this change on what is stored: another page of this view may have folded others since.
      const all = saved();
      if (shut) all.add(sid); else all.delete(sid);
      keep(FOLDS, JSON.stringify([...all].filter((id) => !id || sections.some((s) => s.id === id))));
      render();
    };
    // Folded: the chats shown under it by the tabs' rules, unread and @, as a count and a mark.
    const header = (s, rows, shut, sel, actions) => {
      const sid = s ? s.id : "", li = el("li", "sect" + (sel === "§" + sid ? " sel" : ""));
      li.dataset.sid = sid;
      const b = el("button", "shead");
      b.dataset.k = "head";
      b.setAttribute("aria-expanded", String(!shut));
      const glyph = el("span", "fold", shut ? "▸" : "▾");
      glyph.setAttribute("aria-hidden", "true");  // aria-expanded says it
      b.append(glyph, Object.assign(el("span", "sname truncate", s ? s.name : "Other"), { dir: "auto" }));
      const n = shut ? rows.filter(filters.unread).length : 0;
      if (n) b.append(el("b", "n", n), el("span", "sr", " unread"));
      if (shut && rows.some(filters.mention)) b.append(el("b", "at", "@"), el("span", "sr", " mentioned"));
      b.onclick = () => fold(sid, !shut);
      li.append(b);
      if (s) { const [more, menu] = actions.head(s); li.append(more, ...menu); }
      return li;
    };
    // `shown`: the rows the tab and the filter keep, newest first. Recent: as they are. Sections: under
    // their headers, Manual ones in their stored order, Other last. A search shows only the sections it
    // reaches, unfolded until a fold made during it.
    const items = ({ shown, query, row, actions, sel }) => {
      searching = !!query;
      if (!searching) searched.clear();
      if (!on()) return shown.map(row);
      const filed = new Set(sections.flatMap((s) => s.chats)), byId = new Map(shown.map((r) => [r.id, r]));
      const groups = [...sections.map((s) => {
        const mine = new Set(s.chats);
        return [s, s.order === "manual" ? s.chats.map((id) => byId.get(id)).filter(Boolean) : shown.filter((r) => mine.has(r.id))];
      }), [null, shown.filter((r) => !filed.has(r.id))]];
      shownIn = new Map(groups.map(([s, rows]) => [s?.id, rows.map((r) => r.id)]));
      // A chat selected in a folded section hands the selection to its header: nothing hidden takes keys.
      const hidden = groups.find(([s, rows]) => (searching ? searched : folded).has(s ? s.id : "") && rows.some((r) => r.id === sel));
      if (hidden) select(sel = "§" + (hidden[0]?.id ?? ""));
      // Every item drags but Other, and one with its menu open: a drag would take the mouse from its name box.
      const drags = (li) => { li.draggable = !li.querySelector(".rowmenu") && (li.dataset.sid ?? "x") !== ""; return li; };
      return groups.flatMap(([s, rows]) => {
        if (!rows.length && searching) return [];
        const shut = (searching ? searched : folded).has(s ? s.id : "");
        return [header(s, rows, shut, sel, actions), ...(shut ? [] : rows.map(row))].map(drags);
      });
    };
    // The Recent / Sections switch, in `box`, shown once a section exists.
    const paint = (box) => {
      const k = box.contains(document.activeElement) && document.activeElement.dataset.k;  // a key press on it keeps it
      box.hidden = !sections.length;
      box.replaceChildren(...[["recent", "Recent"], ["sections", "Sections"]].map(([k, label]) => {
        const b = el("button", mode === k ? "on" : null, label);
        b.dataset.k = "mode:" + k;
        b.setAttribute("aria-pressed", String(mode === k));
        b.onclick = () => { mode = k; keep(MODE, k); render(); };
        return b;
      }));
      if (k) (box.hidden ? filter : box.querySelector(`[data-k="${k}"]`))?.focus();
    };
    // The focus once an item went: on `key`'s item, else on the filter box.
    const land = (key) => { select(key || ""); render(); (itemOf(list, key)?.querySelector(".name, .shead") || filter).focus(); };
    // ←/→ on an item: a header folds and unfolds, ← on a chat reaches its header. The item to select, or null.
    // Modified arrows stay the browser's: Alt+← is Back, which the chat page follows.
    const side = (li, e) => {
      const key = e.key;
      if (!on() || !li || (key !== "ArrowLeft" && key !== "ArrowRight") || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return null;
      if (li.dataset.sid === undefined) {
        if (key !== "ArrowLeft") return null;
        let h = li.previousElementSibling;
        while (h && h.dataset.sid === undefined) h = h.previousElementSibling;
        return h;
      }
      fold(li.dataset.sid, key === "ArrowLeft");
      return itemOf(list, "§" + li.dataset.sid);
    };
    // Alt+↑/↓: a chat moves past the next chat shown in its Manual section (hidden ones would swallow a
    // ±1 step); false when it is in none.
    const nudge = (id, by) => {
      const s = on() && holder(id);
      if (s?.order !== "manual") return false;
      const seen = shownIn.get(s.id) || [], next = seen[seen.indexOf(id) + by];
      if (seen.includes(id) && next) send({ section_chat_move: id, to: s.chats.indexOf(next) });
      return true;
    };
    // Drag and drop: `drag` is the dragged item's key. A chat goes into the section of the header or chat
    // it is dropped on, at the drop position in a Manual section; a header lands before the header it is
    // dropped on, last on Other. `target` → [verb, item to mark, mark] or null.
    let drag = "", mark = null;
    const unmark = () => { mark?.classList.remove("dropin", "dropup", "dropdown"); mark = null; };
    const target = (li, e) => {
      const sid = li?.dataset.sid, id = li?.dataset.id, to = sid ? sections.find((x) => x.id === sid) : undefined;
      // Renders wait for the drag: a section another view deleted meanwhile is still on screen, no target.
      if (!drag || (sid === undefined && id === undefined) || (sid && !to)) return null;
      if (drag[0] === "§") {
        // A header deleted mid-drag, or one dropped where it already is, moves nothing.
        const h = drag.slice(1), now = sections.findIndex((s) => s.id === h), ids = sections.map((s) => s.id).filter((x) => x !== h);
        const at = sid ? ids.indexOf(sid) : ids.length;
        return sid === undefined || sid === h || now < 0 || at === now ? null : [{ section_move: h, to: at }, li, "dropup"];
      }
      const from = holder(drag), s = sid !== undefined ? to : holder(id), head = itemOf(list, "§" + (s?.id ?? ""));
      if (!s) return from ? [{ section_file: drag, section: null }, head, "dropin"] : null;
      const manual = s.order === "manual";
      if (sid !== undefined && from === s) return manual && s.chats[0] !== drag ? [{ section_chat_move: drag, to: 0 }, head, "dropin"] : null;
      if (sid !== undefined || !manual) return from === s ? null : [{ section_file: drag, section: s.id }, head, "dropin"];
      const r = li.getBoundingClientRect(), below = e.clientY > r.top + r.height / 2, at = s.chats.filter((c) => c !== drag).indexOf(id) + below;
      if (id === drag || (from === s && at === s.chats.indexOf(drag))) return null;
      return [from === s ? { section_chat_move: drag, to: at } : { section_file: drag, section: s.id, at }, li, below ? "dropdown" : "dropup"];
    };
    const stop = () => { drag = ""; unmark(); render(); };
    list.addEventListener("dragstart", (e) => {
      const li = e.target.closest?.("li[draggable=true]");
      if (!li || !on()) return;
      drag = itemKey(li);
      // Only the private type: the title link's URL would land as text in the composer. Firefox starts no drag without data.
      e.dataTransfer.clearData();
      e.dataTransfer.setData("application/x-miniteams-item", drag);
    });
    list.addEventListener("dragover", (e) => {
      const t = target(e.target.closest?.("li"), e);
      unmark();
      if (!t) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      mark = t[1]; mark?.classList.add(t[2]);
    });
    list.addEventListener("dragleave", unmark);
    list.addEventListener("drop", (e) => {
      const t = target(e.target.closest?.("li"), e);
      if (t) { e.preventDefault(); send(t[0]); }
      if (drag) stop();
    });
    list.addEventListener("dragend", stop);
    // A source removed mid-drag fires its dragend off the list: the first move with no button ends the drag.
    addEventListener("pointermove", (e) => { if (drag && e.buttons === 0) stop(); });
    return { set, list: () => sections, holder, items, paint, side, nudge, land, dragging: () => !!drag };
  };

  // A textarea exposes no caret coordinates: lay the text out in a hidden copy, the caret's word whole (it wraps with it).
  const caret = (input, at = input.selectionStart) => {
    const cs = getComputedStyle(input), copy = el("div", null, input.value.slice(0, at)), mark = el("span", null, input.value.slice(at).match(/^\S*/)[0] || "​");
    for (const k of ["font", "padding", "border", "boxSizing", "lineHeight", "letterSpacing", "tabSize", "width"]) copy.style[k] = cs[k];
    Object.assign(copy.style, { position: "absolute", visibility: "hidden", whiteSpace: "pre-wrap", overflowWrap: "break-word", top: 0, left: 0 });
    copy.append(mark); document.body.append(copy);
    const xy = [mark.offsetLeft, mark.offsetTop, mark.offsetHeight];
    copy.remove();
    return xy;
  };
  // A wrapped line counts as several: the caret is on the first one the reader sees.
  const firstLine = (input) => caret(input)[1] === caret(input, 0)[1];

  // `:` / `@` completion (spec 012) on `input`, its list in `pop`. The pages differ only in the hooks:
  // `chat()` the chat the text goes to, `put(text, from, to, item)` the insertion, `edited()` after it, and
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
    // Spec 033: each pick anchored at the `@` it was inserted at, in the text it was last kept current with.
    // The server reads that `@` as that person while one of their forms follows it, else as unpicked: an
    // edit never moves a pick to another `@`, it can only drop it.
    const picked = new Map();  // chat id → { text, picks: [{ name, mri, everyone?, at }] }
    const token = () => {
      const at = input.selectionStart;
      if (at !== input.selectionEnd) return null;
      // Word start only: `10:30` and `me@host` must not ask anything. One inner space reaches a last name
      // (`@ann l`); an inserted `@Ann Lee ` ends in a second one, so it never asks again.
      // Emoji first: the member pattern's inner space would let `@ann :fi` read as one name.
      const before = input.value.slice(0, at), e = /(?:^|\s):([a-z0-9_+-]{2,})$/i.exec(before);
      const m = e || /(?:^|\s)@([^\s@]*(?: [^\s@]*)?)$/.exec(before);
      if (!m) return null;
      // `@{` asks for the Who of a free label until its `|` (spec 033).
      const brace = !e && m[1].startsWith("{");
      if (brace && /[|}]/.test(m[1])) return null;
      return { kind: e ? "emoji" : "member", q: brace ? m[1].slice(1) : m[1], start: at - m[1].length - 1, brace };
    };
    const render = () => {
      pop.hidden = !comp;
      input.setAttribute("aria-expanded", String(!!comp));
      if (!comp) { pop.replaceChildren(); input.removeAttribute("aria-activedescendant"); return; }
      pop.setAttribute("aria-label", comp.kind === "emoji" ? "emoji" : "members");
      pop.replaceChildren(...comp.items.map((it, i) => {
        const o = el("div");
        o.id = `${pop.id}-${i}`; o.setAttribute("role", "option"); o.setAttribute("aria-selected", String(i === comp.active));
        if (comp.kind === "emoji") {
          // Images only from our own /emoji/ route: the item comes over the socket, the page trusts no URL.
          const own = it.custom && /^\/emoji\/[\w-]+$/.test(it.src || "");
          o.append(own ? Object.assign(el("img"), { src: it.src, alt: "" }) : el("span", "ch", it.custom ? "" : it.char));
        }
        o.append(Object.assign(el("span", "truncate", comp.kind === "emoji" ? `:${it.name}:` : it.name), { dir: "auto" }));
        o.onclick = () => { const t = word(); if (t && t.q === comp.q) { comp.active = i; insert(t); } };
        return o;
      }));
      input.setAttribute("aria-activedescendant", `${pop.id}-${comp.active}`);
      const [x, y, h] = caret(input);
      pop.style.top = input.offsetTop - input.scrollTop + (above ? y - pop.offsetHeight : y + h) + "px";
      pop.style.left = input.offsetLeft + Math.max(0, Math.min(x, input.offsetWidth - pop.offsetWidth)) + "px";
      document.getElementById(`${pop.id}-${comp.active}`).scrollIntoView({ block: "nearest" });
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
    // A change of the text, the old `[a, b)` become the new `[a, caret)` (typed, pasted, deleted, Backspace
    // before it): anchors before stay, after it shift, inside it go. The caret places the change, so `@a @a`
    // losing its first `@a ` keeps the second's anchor. Only a change `beforeinput` announced, or one the page
    // made itself (`own`, its markers shifting anchors as they say), is placed; an undo or any other drops them.
    let from = null;  // where the selection began before the change under way, when `beforeinput` said
    const track = (type, own = input.own) => {
      const s = picked.get(chat()), now = input.value;
      // An announced change whose text comes out the same (`@` typed over itself) still drops what it covers;
      // an unannounced one (Chrome repeats an execCommand's input event) changes nothing.
      if (!s || (s.text === now && from === null)) return;
      const was = s.text, c = input.selectionEnd, d = now.length - was.length, b = c - d;
      let a = 0;
      for (const end = Math.min(c, b, from ?? Infinity); a < end && was[a] === now[a];) a++;
      const ok = (own || from !== null) && !type.startsWith("history") && b >= 0 && b <= was.length && was.slice(b) === now.slice(c);
      s.picks = !ok ? [] : own?.shift ? s.picks.map((p) => ({ ...p, at: own.shift(p.at) }))
        : s.picks.flatMap((p) => (p.at < a ? [p] : p.at >= b ? [{ ...p, at: p.at + d }] : []));
      s.text = now;
    };
    const insert = (t) => {
      const it = comp.items[comp.active], member = comp.kind === "member";
      // The label starts as the name: one the label rule refuses (compose.plain_label, MAX_NAME), and @everyone, go in plain.
      const brace = member && t.brace && !it.everyone && [...it.name].length <= 200 && !/[|}]|(?! )[\p{C}\p{Z}]/u.test(it.name);
      const text = brace ? `@{${it.name}|${it.name}}` : member ? `@${it.name} ` : it.custom ? `:${it.name}:` : it.char;
      const id = chat(), s = picked.get(id) || { text: input.value, picks: [] };
      picked.set(id, s);
      close();
      if (member) dismissed = t.start;  // `@everyone ` + the next word must not ask `everyone t`…
      put(text, t.start, input.selectionStart, it);
      track("insertReplacementText", true);  // a `put` no input event reported (setRangeText)
      if (member) s.picks = [...s.picks.filter((p) => p.at !== t.start), { ...pick(it), at: t.start }];
      if (brace) input.setSelectionRange(t.start + it.name.length + 3, t.start + text.length - 1);  // the label, to type over
      edited();
    };
    // Enter with the list open picks, never sends; true when the key was the list's. While a longer
    // prefix is still being asked, the list on screen answers a shorter one: Enter/Tab wait for it.
    const key = (e) => {
      if (e.isComposing || e.keyCode === 229) return false;
      // Backspace right after a picked full name (and maybe a space) leaves its first name (spec 012): a
      // shortcut, the pick stays anchored at its `@`. Not inside a longer word: `@Ann Leex` is no `@Ann Lee`.
      if (e.key === "Backspace" && !e.ctrlKey && !e.altKey && !e.metaKey && input.selectionStart === input.selectionEnd) {
        const at = input.selectionStart, text = input.value, space = text[at - 1] === " " ? 1 : 0;
        const p = (picked.get(chat())?.text === text ? picked.get(chat()).picks : []).find((p) => !p.everyone && !p.name.startsWith("{")
          && p.at + 1 + p.name.length === at - space && text.startsWith("@" + p.name, p.at));
        const first = p?.name.split(/\s+/)[0].replace(/^[^\p{L}\p{N}_\p{M}]+|[^\p{L}\p{N}_\p{M}]+$/gu, "");
        if (first && first !== p.name && (space || !/^[\p{L}\p{N}_\p{M}]/u.test(text.slice(at)))) {
          close(); dismissed = p.at;
          put(`@${first}${space ? " " : ""}`, p.at, at);
          track("deleteContentBackward", true); edited();
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
    // The picks to send with `text`, each with the offset of its `@` (UTF-16, JavaScript's own): none
    // for a text the anchors were not kept current with, rather than anchors on the wrong `@`.
    const mentions = (text, id = chat()) => {
      const s = picked.get(id);
      return s?.text !== text ? [] : s.picks.filter((p) => text[p.at] === "@").sort((x, y) => x.at - y.at).map((p) => ({ ...pick(p), at: p.at }));
    };
    // Picks back with the text they were sent or saved with (a draft, a failed message, an editor), each
    // with its full identity and anchor; data from sessionStorage or the server, checked like any input.
    const restore = (list, id = chat(), text = input.value) => {
      const ok = list.filter((m) => m && typeof m.name === "string" && typeof m.mri === "string" && [undefined, true].includes(m.everyone)
        && Number.isInteger(m.at));
      picked.set(id, { text, picks: ok.map((m) => ({ ...pick(m), at: m.at })) });
    };
    // A sent text empties the composer without an input event: its dismissed word must not outlive it.
    const forget = (id) => { picked.delete(id); if (id === chat()) dismissed = -1; };
    // An input method's composition is placed once, when it commits: the selection it started over, `[a, b)`,
    // became `[a, b + delta)`, the text around unchanged and the caret at its end, or the anchors go.
    let composing = null;
    const settle = () => {
      const s = picked.get(chat()), [a, b] = composing, now = input.value;
      composing = null;
      if (!s) return;
      const was = s.text, d = now.length - was.length;
      // The commit ends the span: a composition the browser placed elsewhere (its caret not there) is not placed.
      const ok = b + d >= a && input.selectionEnd === b + d && was.slice(0, a) === now.slice(0, a) && was.slice(b) === now.slice(b + d);
      s.picks = !ok ? [] : s.picks.flatMap((p) => (p.at < a ? [p] : p.at >= b ? [{ ...p, at: p.at + d }] : []));
      s.text = now;
    };
    input.addEventListener("compositionstart", () => {
      const s = picked.get(chat());
      if (s && s.text !== input.value) { s.picks = []; s.text = input.value; }  // a write nobody tracked
      composing = [input.selectionStart, input.selectionEnd];
    });
    input.addEventListener("beforeinput", () => {
      if (composing) return;
      const s = picked.get(chat());
      if (s && s.text !== input.value) { s.picks = []; s.text = input.value; }  // a write nobody tracked
      from = input.selectionStart;
      setTimeout(() => { from = null; });  // its input comes in this task; a cancelled one's never does
    });
    input.addEventListener("input", (e) => {
      if (composing && e.isComposing) return;  // placed at its compositionend
      if (composing) {  // a composition that never said it ended: nothing places it
        composing = null;
        const s = picked.get(chat());
        if (s) { s.picks = []; s.text = input.value; }
      } else {
        if (!e.isTrusted) from = null;  // a script's event announces nothing, whatever `beforeinput` came before
        track(e.inputType || "");
      }
      from = null;
      if (!e.isComposing) lookup();
    });
    input.addEventListener("compositionend", () => { if (composing) settle(); lookup(); });
    input.addEventListener("blur", close);
    return { mentions, restore, forget, answered, key, close, reset };
  };

  // execCommand keeps the edit in the textarea's undo history (Ctrl+Z); setRangeText would not.
  // Its `input` event runs the completion's lookup: an insert settles what it dismisses before calling.
  // `adds`: the [offset, length] spans `text` adds to `[a, b)` without changing it (markers, prefixes), which
  // move a mention's anchor with its `@` (spec 033). The input event it causes is marked as the page's own.
  const insertText = (input, text, a, b, selA, selB, adds = null) => {
    input.focus();
    input.setSelectionRange(a, b);
    input.own = { shift: adds && ((at) => at + adds.reduce((n, [pos, len]) => n + (pos <= at ? len : 0), 0)) };
    try {
      if (!document.execCommand("insertText", false, text)) {
        input.setRangeText(text, a, b);
        input.dispatchEvent(new InputEvent("input", { inputType: "insertText" }));
      }
    } finally { input.own = null; }
    input.setSelectionRange(selA, selB);
  };

  // The formatting bar (spec 012, full format) and its preview, the chat page's composer's and each
  // editor's (spec 019): markers around the selection, or around the caret to type into. `bar` holds the
  // [data-fmt] buttons and the [aria-pressed] preview toggle; `view` shows the server's rendering of what
  // `send` would post (compose output, escaped by construction, emoji images on /emoji/). Hooks: `ask(text)`
  // sends a preview ask (false offline), `edited()` after a change, `close()` the completion list. The
  // server answers previews in the order asked: the page hands each answer to its bar (`answered`).
  const MARK = { bold: "**", italic: "*", strike: "~~", code: "`" };
  const formatBar = ({ input, bar, view, ask, edited, close }) => {
    const put = (...a) => insertText(input, ...a);
    const format = (fmt) => {
      if (input.disabled || input.readOnly) return;
      const v = input.value, a = input.selectionStart, b = input.selectionEnd, sel = v.slice(a, b);
      if (fmt === "list" || fmt === "quote") {
        // Whole lines: from the start of the first one to the end of the last (a trailing newline ends it).
        const from = v.lastIndexOf("\n", a - 1) + 1, end = b > a && v[b - 1] === "\n" ? b - 1 : b;
        const nl = v.indexOf("\n", end), to = nl < 0 ? v.length : nl;
        const old = v.slice(from, to).split("\n"), lines = old.map((l) => (fmt === "list" ? "- " : "> ") + l).join("\n");
        const adds = old.map((l, i) => [from + old.slice(0, i).reduce((n, x) => n + x.length + 1, 0), 2]);
        put(lines, from, to, from, from + lines.length, adds);
      } else if (fmt === "code" && sel.includes("\n")) {
        // A fence only opens and closes on a line of its own.
        const pre = a && v[a - 1] !== "\n" ? "\n" : "", post = b < v.length && v[b] !== "\n" ? "\n" : "";
        const text = pre + "```\n" + sel + "\n```" + post;
        put(text, a, b, a, a + text.length, [[a, pre.length + 4], [b, post.length + 4]]);
      } else if (!sel) {
        const m = MARK[fmt];
        put(m + m, a, b, a + m.length, a + m.length, [[a, 2 * m.length]]);
      } else {
        // The server never closes a marker across lines, nor one with a space inside it: each line gets
        // its own pair, inside its list or quote prefix, around its text without the surrounding spaces.
        const m = MARK[fmt], atLineStart = !a || v[a - 1] === "\n", adds = [];
        let inner = null, at = a;  // `at`: where the line being wrapped starts in the text
        const text = sel.split("\n").map((l, i) => {
          const p = i || atLineStart ? /^\s*(?:(?:[-*]|\d+\.|>) +)*/.exec(l)[0] : "";
          const [, lead, body, tail] = /^(\s*)(.*?)(\s*)$/.exec(l.slice(p.length)), open = at + p.length + lead.length;
          at += l.length + 1;
          if (!body) return l;
          adds.push([open, m.length], [open + body.length, m.length]);
          inner ??= [p.length + lead.length + m.length, body.length];
          return p + lead + m + body + m + tail;
        }).join("\n");
        const one = inner && !sel.includes("\n");  // one line: its text stays selected, inside the markers
        put(text, a, b, one ? a + inner[0] : a, one ? a + inner[0] + inner[1] : a + text.length, adds);
      }
      close();
      edited();
    };
    for (const btn of bar.querySelectorAll("[data-fmt]")) btn.onclick = () => format(btn.dataset.fmt);
    // One Tab stop (role=toolbar): ←/→ walk its buttons and move the stop, Home/End reach its ends.
    const tools = [...bar.querySelectorAll("button")];
    tools.forEach((b, i) => { b.tabIndex = i ? -1 : 0; });
    bar.addEventListener("focusin", (e) => { if (tools.includes(e.target)) for (const b of tools) b.tabIndex = b === e.target ? 0 : -1; });
    bar.addEventListener("keydown", (e) => {
      const i = tools.indexOf(e.target), n = tools.length, to = i < 0 ? undefined : { ArrowRight: (i + 1) % n, ArrowLeft: (i + n - 1) % n, Home: 0, End: n - 1 }[e.key];
      if (to === undefined) return;
      e.preventDefault(); tools[to].focus();
    });
    const show = bar.querySelector("[aria-pressed]");
    show.setAttribute("aria-pressed", "false");  // a bar copied from one showing its preview starts off
    // Only the answer to the last ask, while on, shows.
    let showing = false, asked = 0;
    const hide = () => { showing = false; view.hidden = true; view.replaceChildren(); show.setAttribute("aria-pressed", "false"); };
    const shown = (cls, fill) => { view.className = "view " + cls; fill(); view.hidden = false; };
    show.onclick = () => {
      if (showing) { hide(); return; }
      showing = true;
      show.setAttribute("aria-pressed", "true");
      if (ask(input.value)) asked++;
      else shown("err", () => { view.textContent = "offline, no preview"; });
    };
    const answered = (m) => {
      asked = Math.max(0, asked - 1);
      if (!showing || asked) return;  // toggled off, or a later ask (the text changed since) is pending
      if (m.error) shown("err", () => { view.textContent = m.error; });
      else shown("html", () => {
        view.innerHTML = typeof m.preview === "string" ? m.preview : "";
        // A link followed in place would replace the page and its drafts.
        for (const a of view.querySelectorAll("a")) Object.assign(a, { target: "_blank", rel: "noopener" });
      });
    };
    input.addEventListener("input", hide);  // a shown preview is of the text before the change
    // Ctrl+B / Ctrl+I; true when the key was the bar's.
    const key = (e) => {
      const k = e.key.toLowerCase();
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || e.isComposing || e.keyCode === 229 || (k !== "b" && k !== "i")) return false;
      e.preventDefault(); format(k === "b" ? "bold" : "italic");
      return true;
    };
    // Its answer died with the socket: no pressed button over an empty box.
    const lost = () => { if (asked) hide(); asked = 0; };
    return { hide, answered, key, lost };
  };

  // Pasted images (spec 012 phase 5) and attached files (spec 025): queued per chat in the order added,
  // kept in memory only (a reload loses them, the text draft stays), uploaded by the server only when
  // sent. A pasted or dropped image Teams shows inline (IMG_TYPES) takes the inline path, anything else,
  // and whatever the paperclip picks, goes as a file card. Limits mirror images.py and files.py; the
  // server checks the bytes again. Hooks: `chat()` the chat shown, `error(id, text)` a refusal, `busy(id)`
  // its send awaits an answer (the attachments shown are the next message's, never locked). `clip`: the
  // paperclip button, `zone`: where a drop attaches.
  const IMG_MAX = 8, IMG_BYTES = 10 << 20, IMG_TOTAL = 40 << 20, IMG_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
  const FILE_MAX = 10, FILE_BYTES = 100 << 20, FILE_CHUNK = 1 << 20, WIRE_HIGH = 4 << 20;
  const bytes = (n) => n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / (1 << 20)).toFixed(1)} MiB`;
  const imageQueue = ({ input, box, chat, error, busy, clip, zone }) => {
    const queues = new Map(), flight = new Map(), kept = new Map();  // kept: chat id → the attempt of a failed message edited in the composer
    // A refusal is about the images as they were: any change of them clears it, unless the page shows another error since.
    const refused = new Map();
    const refuse = (id, text) => { refused.set(id, text); error(id, text); };
    const unrefuse = (id) => { const was = refused.get(id); if (was === undefined) return; refused.delete(id); error(id, "", was); };
    let chain = Promise.resolve(), adding = 0, last = 0, shown = null;
    const list = (id = chat()) => queues.get(id) || [];
    const view = el("dialog", "imgview"), big = el("img"), bar = el("div"), shut = el("button", null, "Close"), del = el("button", null, "Remove");
    view.setAttribute("aria-label", "image preview");
    bar.append(del, shut); view.append(big, bar); document.body.append(view);
    shut.onclick = () => view.close();
    view.onclick = (e) => { if (e.target === view) view.close(); };  // the backdrop
    view.onclose = () => { shown = null; big.removeAttribute("src"); if (!input.disabled) input.focus(); };
    const remove = (id, it) => {
      if (shown?.[1] === it) view.close();
      URL.revokeObjectURL(it.url);
      queues.set(id, list(id).filter((x) => x !== it));
      kept.delete(id);
      unrefuse(id);
      render();
    };
    del.onclick = () => { if (shown) remove(...shown); };
    const thumb = (id, it, i) => {
      const t = el("span", "thumb"), open = el("button", "open"), x = el("button", "rm", "×");
      open.append(Object.assign(el("img"), { src: it.url, alt: `image ${i}` }));
      open.title = "preview"; open.onclick = () => { shown = [id, it]; big.src = it.url; big.alt = `image ${i}`; view.showModal(); };
      x.title = "remove"; x.setAttribute("aria-label", `remove image ${i}`); x.onclick = () => remove(id, it);
      t.append(open, x);
      return t;
    };
    // A file's name is data, never markup: text nodes only.
    const chip = (id, it) => {
      const c = el("span", "chip"), x = el("button", "rm", "×");
      c.append(Object.assign(el("span", "name", it.file.name), { dir: "auto", title: it.file.name }), el("small", null, bytes(it.file.size)));
      x.title = "remove"; x.setAttribute("aria-label", `remove ${it.file.name}`); x.onclick = () => remove(id, it);
      c.append(x);
      return c;
    };
    const render = () => {
      const id = chat(), items = list(id), off = busy(id);
      box.hidden = !items.length && !off;
      box.classList.toggle("busy", off);
      let n = 0;
      box.replaceChildren(...items.map((it) => it.url ? thumb(id, it, ++n) : chip(id, it)),
        el("span", "temp", off ? "sending…" : n < items.length ? "attachments are lost on reload" : "images are lost on reload"));
    };
    const measure = (url) => new Promise((ok, ko) => {
      const img = new Image();
      img.onload = () => ok([img.naturalWidth, img.naturalHeight]);
      img.onerror = ko;
      img.src = url;
    });
    // One paste after the other: a second Ctrl+V must not overtake the first one's measuring. `docs`:
    // every file goes as a file card (the paperclip), images included.
    const add = (id, files, docs = false) => { adding++; chain = chain.then(async () => {
      for (const file of await files) {
        if (docs || !IMG_TYPES.includes(file.type)) {
          const refusal = list(id).filter((it) => !it.url).length >= FILE_MAX ? `at most ${FILE_MAX} files per message`
            : file.size > FILE_BYTES ? `${file.name}: over ${FILE_BYTES >> 20} MiB` : "";
          if (refusal) { refuse(id, refusal); continue; }
          queues.set(id, [...list(id), { file }]);
          kept.delete(id);
          unrefuse(id);
          render();
          continue;
        }
        const items = list(id).filter((it) => it.url), total = items.reduce((n, it) => n + it.file.size, 0);
        const refusal = items.length >= IMG_MAX ? `at most ${IMG_MAX} images per message`
          : file.size > IMG_BYTES ? `an image is over ${IMG_BYTES >> 20} MiB`
          : total + file.size > IMG_TOTAL ? `images are over ${IMG_TOTAL >> 20} MiB in total` : "";
        if (refusal) { refuse(id, refusal); continue; }
        const url = URL.createObjectURL(file);
        try {
          const [w, h] = await measure(url);
          queues.set(id, [...list(id), { file, url, w, h }]);
          kept.delete(id);
          unrefuse(id);
        } catch (e) { URL.revokeObjectURL(url); refuse(id, "unsupported image: PNG, JPEG, GIF or WebP only"); }
        render();
      }
    }).catch(() => refuse(id, "could not read the pasted image")).finally(() => { adding--; }); };
    // A browser's "Copy image" of a GIF carries its first frame as a PNG and the GIF's address in text/html
    // (Firefox also as text/plain): the https address of a single <img> ending in .gif, else "".
    const gifOf = (data) => {
      const imgs = new DOMParser().parseFromString(data.getData("text/html"), "text/html").images;
      try {
        const url = new URL(imgs.length === 1 ? imgs[0].getAttribute("src") : "");
        return url.protocol === "https:" && /\.gif$/i.test(url.pathname) ? url.href : "";
      } catch { return ""; }
    };
    // The original GIF, fetched by the server (the page reaches only its own origin); else the still.
    const animated = (url, files) => fetch(`/gif?url=${encodeURIComponent(url)}`)
      .then(async (r) => { if (!r.ok) throw new Error(`/gif answered ${r.status}`); return [new File([await r.blob()], "image.gif", { type: "image/gif" })]; })
      .catch((err) => { console.info("pasting the still:", err.message); return files; });
    // Files without text: a text paste that also carries a picture of itself (office suites) stays text.
    input.addEventListener("paste", (e) => {
      const files = [...(e.clipboardData?.files || [])];
      if (!files.length) return;
      const gif = gifOf(e.clipboardData);
      let text = e.clipboardData.getData("text/plain").trim();
      try { text = new URL(text).href; } catch {}  // Firefox's raw address against gifOf's normalized one
      if (text && text !== gif) return;
      e.preventDefault();
      add(chat(), gif ? animated(gif, files) : files);
    });
    if (clip) {
      const pick = Object.assign(el("input"), { type: "file", multiple: true, hidden: true });
      pick.setAttribute("aria-label", "files to attach");
      pick.onchange = () => { add(chat(), [...pick.files], true); pick.value = ""; input.focus(); };
      clip.after(pick);
      clip.onclick = () => pick.click();
    }
    const carries = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
    zone?.addEventListener("dragover", (e) => { if (carries(e) && !input.disabled && !input.readOnly) e.preventDefault(); });
    zone?.addEventListener("drop", (e) => {
      const files = [...(e.dataTransfer?.files || [])];
      if (!files.length || input.disabled || input.readOnly) return;
      e.preventDefault();
      add(chat(), files);
      input.focus();
    });
    // A file dropped next to the composer would replace the page, its drafts and attachments with it.
    addEventListener("dragover", (e) => { if (carries(e) && !e.defaultPrevented) { e.preventDefault(); e.dataTransfer.dropEffect = "none"; } });
    addEventListener("drop", (e) => { if (carries(e)) e.preventDefault(); });
    // Any edit makes the composer's message a new one: an identical text must not be looked up as the old one.
    input.addEventListener("input", () => kept.delete(chat()));
    addEventListener("pagehide", (e) => { if (!e.persisted) for (const q of queues.values()) for (const it of q) URL.revokeObjectURL(it.url); });
    // The send verb for `id` gets its images, files and a message id, then each image follows as one
    // binary frame, then each file in FILE_CHUNK frames, in order. `sent`: the queued message's last
    // attempt; the very same message again keeps its id and asks the server to look for it first (Teams
    // does not dedupe, and any failure may hide a posted message); an edited one is a new message, so an
    // edit is never swallowed by the check. False when nothing is left to send (every pasted image
    // refused), for the page to settle. Resolves once the verb is out; the files stream on after it.
    // One send on the wire at a time: the server takes binary frames as the latest send's.
    let wire = Promise.resolve();
    const send = (ws, id, verb, items, sent) => new Promise((ok) => { wire = wire.then(async () => {
      if (ws?.readyState !== 1) return ok(true);  // closing: the page's `lost` requeues the message, attachments included
      if (!items.length && !verb.text.trim()) return ok(false);
      const json = JSON.stringify(verb), same = sent?.json === json;  // attachments added or removed already dropped the attempt
      const cmid = same ? sent.cmid : String(last = Math.max(Date.now(), last + 1));
      const pics = items.filter((it) => it.url), docs = items.filter((it) => !it.url), f = { cmid, json, items };
      flight.set(id, f);
      ws.send(JSON.stringify({ ...verb, cmid, ...(same ? { check: true } : {}),
        ...(pics.length ? { images: pics.map((it) => ({ w: it.w, h: it.h, size: it.file.size })) } : {}),
        ...(docs.length ? { files: docs.map((it) => ({ name: it.file.name, size: it.file.size })) } : {}) }));
      ok(true);
      // Bytes, not Blobs: Firefox sends Blobs as they finish reading, a smaller one overtaking. Paced on
      // the socket's buffer: 1 GiB of files must not queue in memory. A failure, a dropped socket or a
      // retry ends this attempt's stream.
      const parts = [...pics.map((it) => it.file), ...docs.flatMap((it) => Array.from({ length: Math.ceil(it.file.size / FILE_CHUNK) },
        (_, i) => it.file.slice(i * FILE_CHUNK, (i + 1) * FILE_CHUNK)))];
      for (const part of parts) {
        while (ws.readyState === 1 && ws.bufferedAmount > WIRE_HIGH) await new Promise((r) => setTimeout(r, 20));
        const data = await part.arrayBuffer();
        if (ws.readyState !== 1 || f.stop) return;
        ws.send(data);
      }
    }).catch(() => ok(true)); });
    // Success drops the attachments sent.
    const settled = (id) => {
      for (const it of flight.get(id)?.items || []) URL.revokeObjectURL(it.url);
      flight.delete(id);
      render();
    };
    // The chat's images, once those pasted before this call are measured, detached for one message.
    const take = (id) => { kept.delete(id); return chain.then(() => { const items = list(id); queues.delete(id); render(); return items; }); };
    // A failed message edited in the composer: its images go back in front of any pasted since, its
    // attempt is kept until the composer changes (`again`): sent unchanged, it is a retry.
    const back = (id, items, sent) => { queues.set(id, [...items, ...list(id)]); if (sent) kept.set(id, sent); render(); };
    // A failure or a dropped socket: the send in flight, its attempt and images, handed back to its queued message.
    const recall = (id) => { const f = flight.get(id); if (f) f.stop = true; flight.delete(id); return f; };
    // The send in flight: a reload keeps its attempt with its queued message.
    const attempt = (id) => { const f = flight.get(id); return f ? { cmid: f.cmid, json: f.json } : undefined; };
    // A stored draft's kept attempt (data from sessionStorage), checked like any input.
    const arm = (id, t) => { if (/^\d+$/.test(t?.cmid) && typeof t.json === "string") kept.set(id, { cmid: t.cmid, json: t.json }); };
    return { list, send, settled, take, back, again: (id) => kept.get(id), arm, render, attempt, recall, adding: () => adding > 0 };
  };

  // Burst sends (spec 017): per chat, one message in flight, the rest in send order. `push` takes the
  // chat's images (`imgs.take`) with the item's verb; the page reports the server's answer with `done` /
  // `fail`. A failure pauses the chat's queue with its item still at the head, `failed`, until ↻ `retry`,
  // ✎ `edit` or ✕ `discard` (`draw` shows the items and their buttons). Hooks: `socket()` the live
  // socket, `nothing(id)` an item with nothing to send (every image refused), `changed(id)` any change,
  // to redraw, `note(id, text)` a word for the chat's error line, `edit(id, item)` true when the page took
  // the item into its empty composer (else it is edited in place), `acked(id, item)` an item sent. Item
  // states: queued, sending, failed, paused (queued behind a failure or an edit). The queues survive a
  // reload in sessionStorage (`save` / `load`), images aside.
  const sendQueue = ({ imgs, socket, nothing, changed, note, edit: took, acked = () => {} }) => {
    const queues = new Map();  // chat id → { list: [{ verb, images, state, …page fields }], paused }
    const of = (id) => { let q = queues.get(id); if (!q) queues.set(id, q = { list: [], paused: false }); return q; };
    const list = (id) => queues.get(id)?.list || [];
    const busy = (id) => list(id)[0]?.state === "sending";
    const failed = (id) => list(id).some((e) => e.state === "failed");
    const pump = (id) => {
      const q = of(id), e = q.list[0];
      if (q.paused || !e || e.state === "sending" || socket()?.readyState !== 1) return;  // offline: `wake` resumes
      // Requeued, dropped or pumped again (a socket that dropped and came back) while its images were
      // measured: only this pump's own, still current, sends.
      const go = e.go = {}, mine = () => e.go === go && e.state === "sending" && q.list[0] === e;
      e.state = "sending"; changed(id);
      e.images.then((items) => !mine() || imgs.send(socket(), id, e.verb, items, e.sent)).then((sent) => {
        if (sent && mine()) e.cmid = imgs.attempt(id)?.cmid ?? e.cmid;  // the chat page matches Teams' echo on it
        if (sent || !mine()) return;
        q.list.shift(); nothing(id); changed(id); pump(id);
      });
    };
    // `front`: the message edited in the composer goes back to its place. Unchanged, it keeps its attempt.
    const push = (id, item, front = false) => {
      const q = of(id), pics = imgs.list(id).length > 0 || imgs.adding(), was = !imgs.adding() && imgs.again(id);
      const e = { ...item, pics, images: imgs.take(id), state: q.paused ? "paused" : "queued" };
      // Its bubble or line names its files.
      e.images.then((items) => { e.files = items.filter((it) => !it.url).map((it) => it.file.name); if (e.files.length) changed(id); });
      if (was && was.json === JSON.stringify(item.verb)) Object.assign(e, { sent: was, cmid: was.cmid });  // its echo may be on screen
      if (front) q.list.unshift(e); else q.list.push(e);
      changed(id);
    };
    // A failed item waits for its own action: nothing behind it goes first.
    const resume = (id) => {
      const q = of(id);
      if (failed(id)) { changed(id); return; }
      q.paused = false;
      for (const e of q.list) if (e.state === "paused") e.state = "queued";
      changed(id); pump(id);
    };
    const done = (id) => {
      if (!busy(id)) return;
      const e = of(id).list.shift();
      imgs.settled(id); acked(id, e); changed(id); pump(id);
    };
    // The send in flight, its attempt and images, back on its item: sent again, it goes with its id and the check.
    const hold = (id, e) => { const f = imgs.recall(id); if (f) Object.assign(e, { sent: { cmid: f.cmid, json: f.json }, images: Promise.resolve(f.items) }); };
    // A mention the server refused (spec 033: ambiguous, unknown…, said with compose.py's `\@` hint) is
    // the text's to fix: it goes back into the empty composer, its reason on the chat's line.
    const fail = (id, text) => {
      if (!busy(id)) return;
      const q = of(id), e = q.list[0];
      hold(id, e);
      Object.assign(e, { state: "failed", why: text });
      q.paused = true;
      for (const x of q.list.slice(1)) x.state = "paused";
      changed(id);
      if (text.endsWith(" (\\@ for a literal @)") && back(id, e)) note(id, "not sent: " + text);
    };
    // No answer will come once the socket drops, and the message may have gone out: it waits at the head,
    // queued, and goes again with its id and the check once the socket is back (`wake`).
    const lost = () => {
      for (const [id, q] of queues) {
        const e = q.list[0];
        if (e?.state !== "sending") continue;
        hold(id, e);
        e.state = "queued";
        changed(id);
      }
    };
    const retry = (id, e) => { e.state = "queued"; e.editing = undefined; resume(id); };
    const discard = (id, e) => {
      const q = of(id);
      q.list = q.list.filter((x) => x !== e);
      e.images.then((items) => { for (const it of items) URL.revokeObjectURL(it.url); });
      resume(id);
    };
    const wake = () => { for (const id of [...queues.keys()]) pump(id); };
    // Into the page's empty composer, its images too: the queue stays paused until the composer sends it
    // (back at its place) or Enter on the emptied box drops it. Else an editor in the item.
    const back = (id, e) => {
      if (!took(id, e)) return false;
      const q = of(id);
      q.list = q.list.filter((x) => x !== e);
      e.images.then((items) => imgs.back(id, items, e.sent));
      changed(id);
      return true;
    };
    const edit = (id, e) => { if (!back(id, e)) { e.editing = e.verb.text; changed(id); } };
    // An edit sent from the item: a changed text is a new message (its old id's echo is not its own), an
    // unchanged one a retry. That editor tracks no anchors (spec 033): picks before the first change stay.
    const resend = (id, e, text) => {
      if (text !== e.verb.text) {
        let a = 0;
        while (text[a] !== undefined && text[a] === e.verb.text[a]) a++;
        e.verb = { ...e.verb, text, mentions: e.verb.mentions.filter((m) => m.at < a) }; e.cmid = undefined;
      }
      retry(id, e);
    };
    const LABEL = { queued: "queued", sending: "sending…", paused: "paused", sent: "sent" };
    const button = (label, title, act) => {
      const b = el("button", null, label);
      b.title = title; b.setAttribute("aria-label", title); b.onclick = act;
      return b;
    };
    // One node per item, kept across redraws: an edit in progress keeps its text and focus. Plain text only.
    const look = (id, e) => {
      if (!e.node) {
        const acts = el("span", "qacts");
        acts.append(button("↻", "retry", () => retry(id, e)), button("✎", "edit", () => edit(id, e)), button("✕", "discard", () => discard(id, e)));
        e.node = el("div", "q");
        e.node.append(Object.assign(el("div", "qtext"), { dir: "auto" }), el("span", "qstate"), acts);
      }
      const [text, label, acts, open] = [".qtext", ".qstate", ".qacts", "textarea"].map((k) => e.node.querySelector(k));
      e.node.className = "q " + e.state;
      text.textContent = [e.verb.text, e.files?.length ? "📎 " + e.files.join(", ") : ""].filter(Boolean).join(" ") || "🖼";
      label.textContent = label.title = e.state === "failed" ? "failed: " + e.why : LABEL[e.state];
      acts.hidden = e.state !== "failed" || e.editing !== undefined;
      text.hidden = e.editing !== undefined;
      if (e.editing === undefined) open?.remove();
      else if (!open) {
        const box = Object.assign(el("textarea"), { value: e.editing, rows: Math.min(8, e.editing.split("\n").length) });
        box.setAttribute("aria-label", "edit the failed message");
        box.oninput = () => { e.editing = box.value; };
        // Its redraw may drop the item (echoed meanwhile on the chat page): the focus goes to the composer then.
        const cancel = () => { e.editing = undefined; changed(id); (e.node.isConnected ? e.node.querySelector("button[title=edit]") : document.getElementById("reply")).focus(); };
        // Enter on an emptied text without images: nothing to send, the edit is cancelled.
        box.onkeydown = (ev) => {
          if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); cancel(); return; }
          if (ev.key !== "Enter" || ev.shiftKey || ev.isComposing || ev.keyCode === 229) return;
          ev.preventDefault();
          if (box.value.trim() || e.pics) resend(id, e, box.value); else cancel();
        };
        text.after(box);
        box.focus();
      }
      return e.node;
    };
    // `items` in `box`, in order; nodes only move when the list changed around them.
    const draw = (box, id, items) => {
      const want = items.map((e) => look(id, e));
      for (const n of [...box.children]) if (!want.includes(n)) n.remove();
      want.forEach((n, i) => { if (box.children[i] !== n) box.insertBefore(n, box.children[i] || null); });
    };
    // Before a reload: each chat's queue, in order; the message in flight keeps its attempt (it may have
    // gone out), so it goes again with the same id and the check; a failed one its reason. Emptied: an
    // answer arriving meanwhile is moot.
    const KEY = "queue:";
    const save = () => {
      for (const [id, q] of queues) {
        const items = q.list.splice(0).map((e) => ({ verb: e.verb, quotes: e.quotes, pics: e.pics,
          sent: e.state === "sending" ? imgs.attempt(id) : e.sent, ...(e.state === "failed" ? { why: e.why } : {}) }));
        try { if (items.length) sessionStorage.setItem(KEY + id, JSON.stringify({ paused: q.paused, items })); } catch (e) {}
      }
      queues.clear();
    };
    // Data from sessionStorage, checked like any input: a malformed entry is dropped, never thrown on.
    // The verb is kept as parsed, so an unchanged message serialises to its attempt's `json` again.
    const str = (v) => typeof v === "string", keys = (o, ok) => !!o && typeof o === "object" && !Array.isArray(o) && Object.keys(o).every((k) => ok.includes(k));
    const pick = (m) => keys(m, ["name", "mri", "everyone", "at"]) && str(m.name) && str(m.mri) && [undefined, true].includes(m.everyone)
      && Number.isInteger(m.at) && m.at >= 0;
    const verbOk = (v, id) => keys(v, ["send", "text", "mentions", "format", "quote"]) && v.send === id && str(v.text) && str(v.format)
      && Array.isArray(v.mentions) && (v.quote === undefined || Array.isArray(v.quote) && v.quote.every(str));
    const LOST = "attachments not kept across the reload: add them again";
    // `only`: the chats to take (null: all), the others' queues stay stored.
    const load = (only = null) => {
      let stored = [];
      try { stored = Object.keys(sessionStorage).filter((k) => k.startsWith(KEY) && (!only || only.includes(k.slice(KEY.length)))); } catch (e) {}
      for (const k of stored) {
        const id = k.slice(KEY.length);
        let rec = null;
        try { rec = JSON.parse(sessionStorage.getItem(k)); sessionStorage.removeItem(k); } catch (e) {}
        if (!Array.isArray(rec?.items)) continue;
        const q = of(id);
        let pics = false;
        q.paused = rec.paused === true;
        for (const it of rec.items) {
          if (!verbOk(it?.verb, id)) continue;
          pics ||= it.pics === true;
          if (!it.verb.text.trim()) continue;
          // A pick that is not one (saved before anchors, spec 033) is left out, never its message: that
          // `@` then resolves on the roster, or the send is refused. Its attempt stays its own, check and all.
          const verb = { ...it.verb, mentions: it.verb.mentions.filter(pick) }, same = verb.mentions.length === it.verb.mentions.length;
          const sent = /^\d+$/.test(it.sent?.cmid) && str(it.sent.json) ? { cmid: it.sent.cmid, json: same ? it.sent.json : JSON.stringify(verb) } : undefined;
          const quotes = (Array.isArray(it.quotes) ? it.quotes : []).filter((qt) => str(qt?.id)).map((qt) => ({ id: qt.id, name: String(qt.name ?? ""), line: String(qt.line ?? "") }));
          // A failed head waits for its ↻ ✎ ✕ again. `cmid`: its echo may land before the resend.
          const why = q.paused && !q.list.length && str(it.why) ? it.why : undefined;
          q.list.push({ verb, quotes, sent, cmid: sent?.cmid, pics: false, images: Promise.resolve([]),
            ...(why !== undefined ? { state: "failed", why } : { state: q.paused ? "paused" : "queued" }) });
        }
        if (!q.list.length) q.paused = false;
        const held = q.paused && !failed(id);
        const notes = [...(pics ? [LOST] : []), ...(held ? [`${q.list.length} queued: Enter sends them`] : [])];
        if (notes.length) note(id, notes.join(" · "));
        changed(id);
      }
    };
    return { list, busy, held: (id) => !!queues.get(id)?.paused && !failed(id), push, resume, done, fail, lost, discard, wake, save, load, draw };
  };
