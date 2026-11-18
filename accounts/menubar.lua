-- claude-accounts menu bar status.
-- Loaded by Hammerspoon from your init.lua:
--   dofile("<path to the latidos clone>/accounts/menubar.lua")
-- It finds claude_accounts.py next to itself.
--
-- One asterisk in the menu bar for the Claude.ai logins of the accounts config: green when every login has
-- more than soon_days left, amber with the days left when one expires soon, red with "!" when one
-- expired, is missing or holds another user's login, grey when it could not check. The menu lists
-- each account with its expiry date (click: renew it in its Chrome profile), the warm-up of the day,
-- "Renovar las caducadas", "Renovar todas" and "Comprobar ahora". From notify_days before expiry a
-- macOS notification per account and day offers the renewal. Status comes from
-- `claude_accounts.py status --json` every 5 minutes, on wake and on demand. Globals keep objects alive.

local HOME = os.getenv("HOME")
local HERE = (debug.getinfo(1, "S").source:match("^@(.*)/[^/]*$")) or (HOME .. "/code/latidos/accounts")
local TOOL = HERE .. "/claude_accounts.py"
local PYTHON = "/opt/homebrew/bin/python3"
local ENV = {
    HOME = HOME,
    USER = os.getenv("USER") or "",
    LOGNAME = os.getenv("USER") or "",
    LANG = "en_US.UTF-8",
    PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
}
local REFRESH_SECONDS = 300
local STATUS_TIMEOUT = 60

local COLORS = {
    ok = { red = 0.20, green = 0.70, blue = 0.35 },
    soon = { red = 0.95, green = 0.60, blue = 0.05 },
    bad = { red = 0.90, green = 0.22, blue = 0.20 },
    unknown = { red = 0.55, green = 0.55, blue = 0.55 },
}
local BAD = { expired = true, missing = true, wrong = true }
local DAYS = { "dom", "lun", "mar", "mié", "jue", "vie", "sáb" }
local MONTHS = { "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic" }

claudeAccounts = claudeAccounts or {}
local M = claudeAccounts
M.last, M.updated, M.updatedAt, M.busy, M.error = nil, nil, nil, false, nil
M.renewing = M.renewing or {}

if M.menu then M.menu:delete() end
M.menu = hs.menubar.new()

local function colorOf(state)
    if state == "ok" then return "ok" end
    if state == "soon" then return "soon" end
    if BAD[state] then return "bad" end
    return "unknown"
end

-- "lun 17 nov, 23:31" from an ISO time with offset (the tool writes local time).
local function dateText(iso)
    if not iso then return "?" end
    local y, mo, d, h, mi = iso:match("^(%d+)-(%d+)-(%d+)T(%d+):(%d+)")
    if not y then return iso end
    local t = os.time({ year = tonumber(y), month = tonumber(mo), day = tonumber(d), hour = 12 })
    return string.format("%s %d %s, %s:%s", DAYS[tonumber(os.date("%w", t)) + 1], tonumber(d), MONTHS[tonumber(mo)], h, mi)
end

local function daysText(days)
    if not days then return "" end
    if days < 0 then return "caducada" end
    if days < 1 then return string.format("%d h", math.floor(days * 24 + 0.5)) end
    return string.format("%d d", math.floor(days))
end

local function stale()
    return M.error ~= nil or not M.updatedAt or (os.time() - M.updatedAt) > 2 * REFRESH_SECONDS
end

local function overall(data)
    if not data then return "unknown", nil end
    local worst, minDays = "ok", nil
    for _, a in ipairs(data.accounts or {}) do
        local c = colorOf(a.state)
        if c == "bad" then worst = "bad"
        elseif c == "soon" and worst ~= "bad" then worst = "soon"
        elseif c == "unknown" and worst == "ok" then worst = "unknown" end
        if a.state == "soon" and a.days_left and (not minDays or a.days_left < minDays) then minDays = a.days_left end
    end
    return worst, minDays
end

local function render()
    local state, minDays = "unknown", nil
    if M.last and not stale() then state, minDays = overall(M.last) end
    local title = "✳︎"
    if state == "bad" then title = "✳︎!" elseif state == "soon" then title = "✳︎" .. daysText(minDays):gsub(" ", "") end
    M.menu:setTitle(hs.styledtext.new(title, { color = COLORS[state], font = { name = ".AppleSystemUIFont", size = 14 } }))
    local tip = "Claude: " .. ({ ok = "logins al día", soon = "un login caduca pronto", bad = "hay logins caducados", unknown = "sin comprobar" })[state]
    if M.error then tip = tip .. "\n" .. M.error end
    M.menu:setTooltip(tip)
end

M.tasks = M.tasks or {}

local function runTool(args, onDone)
    local all = { TOOL }
    for _, a in ipairs(args) do table.insert(all, a) end
    local task
    task = hs.task.new(PYTHON, function(code, out, err)
        M.tasks[task] = nil
        if onDone then onDone(code, out or "", err or "") end
    end, all)
    if not task then return nil end
    task:setEnvironment(ENV)
    if not task:start() then return nil end
    M.tasks[task] = true
    return task
end

-- One notification per account and day once a login is close to expiring (or expired).
local function notifyDue(data)
    local within = (data.settings and data.settings.notify_days) or 2
    local today = os.date("%Y-%m-%d")
    local sent = hs.settings.get("claudeAccounts.notified") or {}
    for _, a in ipairs(data.accounts or {}) do
        local due = BAD[a.state] or (a.days_left and a.days_left <= within)
        if due and sent[a.tag] ~= today and not a.renewing then
            sent[a.tag] = today
            local text = BAD[a.state] and (a.detail ~= "" and a.detail or a.state)
                or ("caduca " .. dateText(a.expires_at) .. " (" .. daysText(a.days_left) .. ")")
            hs.notify.new(function() M.login({ a.tag }, a.label) end, {
                title = "Claude · " .. a.label,
                informativeText = text .. ". Clic para renovar en Chrome.",
                hasActionButton = true, actionButtonTitle = "Renovar", withdrawAfter = 0,
            }):send()
        end
    end
    hs.settings.set("claudeAccounts.notified", sent)
end

local function finishRefresh(err)
    M.busy = false
    if M.watchdog then M.watchdog:stop(); M.watchdog = nil end
    M.error = err
    render()
end

function M.refresh()
    if M.busy then return end
    M.busy = true
    render()
    M.gen = (M.gen or 0) + 1
    local gen = M.gen
    M.task = runTool({ "status", "--json" }, function(code, out, err)
        -- a run the watchdog already gave up on, or one replaced by a newer refresh: ignore it
        if gen ~= M.gen or not M.busy then return end
        local ok, data = pcall(hs.json.decode, out)
        if ok and type(data) == "table" and data.accounts then
            M.last, M.updated, M.updatedAt = data, os.date("%H:%M"), os.time()
            finishRefresh(nil)
            notifyDue(data)
        else
            local msg = ((err ~= "" and err or out):gsub("%s+$", "")):sub(1, 200)
            finishRefresh(msg ~= "" and msg or ("la comprobación terminó sin datos (código " .. tostring(code) .. ")"))
        end
    end)
    if not M.task then
        finishRefresh("no se pudo lanzar " .. PYTHON)
        return
    end
    M.watchdog = hs.timer.doAfter(STATUS_TIMEOUT, function()
        if gen ~= M.gen then return end
        M.watchdog = nil
        finishRefresh("la comprobación no terminó en " .. STATUS_TIMEOUT .. " s")
        if M.task and M.task:isRunning() then M.task:terminate() end
    end)
end

-- Renew logins (tags, or a flag such as "--expired" / "--all"); one Chrome window per account.
function M.login(args, what)
    for _, t in ipairs(args) do
        if M.renewing[t] then
            hs.alert.show("Claude: ya hay una renovación de " .. what .. " abierta")
            return
        end
    end
    for _, t in ipairs(args) do M.renewing[t] = true end
    hs.alert.show("Claude: renovando " .. what .. " (se abre Chrome, pulsa Autorizar)")
    local started = runTool({ "login", table.unpack(args) }, function(code, out, err)
        for _, t in ipairs(args) do M.renewing[t] = nil end
        -- the tool prints one line per account ("Work: renovada") or "nada que renovar"
        local result = out:gsub("%s+$", "")
        if result == "" then result = (err:gsub("%s+$", "")):sub(1, 200) end
        if result == "" then result = "terminó con código " .. tostring(code) end
        local head = ({ [0] = "Claude", [2] = "Claude: ¡usuario equivocado!" })[code] or "Claude: no todo salió bien"
        hs.alert.show(head .. "\n" .. result, code == 0 and 3 or 6)
        M.refresh()
    end)
    if not started then
        for _, t in ipairs(args) do M.renewing[t] = nil end
        hs.alert.show("Claude: no se pudo lanzar " .. PYTHON)
    end
end

local function warmText(w)
    if not w then return "sin calentar" end
    local last = w.last_success_at and w.last_success_at:sub(1, 10) == os.date("%Y-%m-%d")
    if w.outcome == "blocked" and (w.last_attempt_at or "") > (w.last_success_at or "") then return "límite agotado" end
    if w.outcome == "error" and (w.last_attempt_at or "") > (w.last_success_at or "") then return "falló el calentamiento" end
    if w.last_success_at then return (last and "hoy " or (w.last_success_at:sub(9, 10) .. "/" .. w.last_success_at:sub(6, 7) .. " ")) .. w.last_success_at:sub(12, 16) end
    return "sin calentar"
end

local function menuItems()
    local items = {}
    local header = "Claude · logins"
    if M.busy then header = header .. " · comprobando…"
    elseif M.error and M.updated then header = header .. " · error, último OK " .. M.updated
    elseif M.updated then header = header .. " · " .. M.updated end
    table.insert(items, { title = header, disabled = true })
    table.insert(items, { title = "-" })
    if M.error then table.insert(items, { title = "Error: " .. M.error, disabled = true }) end
    local data = M.last or { accounts = {} }
    local due = 0
    local warm = {}
    for _, a in ipairs(data.accounts) do
        local renewing = a.renewing or M.renewing[a.tag]
        local text
        if BAD[a.state] then
            text = a.label .. "  ·  " .. (a.detail ~= "" and a.detail or a.state) .. "  ·  renovar"
            due = due + 1
        elseif a.expires_at then
            text = a.label .. "  ·  caduca " .. dateText(a.expires_at) .. "  (" .. daysText(a.days_left) .. ")"
        else
            text = a.label .. "  ·  " .. (a.detail ~= "" and a.detail or a.state)
        end
        if renewing then text = text .. "  ·  renovando…" end
        table.insert(items, {
            title = hs.styledtext.new("● ", { color = COLORS[colorOf(a.state)] }) .. hs.styledtext.new(text),
            tooltip = a.email .. (a.login_email and a.login_email ~= a.email and ("\nLogin actual: " .. a.login_email) or "")
                .. "\nClic: renovar el login en Chrome (perfil de " .. a.label .. ")",
            fn = function() M.login({ a.tag }, a.label) end,
            disabled = renewing and true or false,
        })
        table.insert(warm, a.tag .. " " .. warmText(a.warmup))
    end
    if #warm > 0 then
        table.insert(items, { title = "-" })
        table.insert(items, { title = "Calentador: " .. table.concat(warm, " · "), disabled = true })
    end
    table.insert(items, { title = "-" })
    table.insert(items, {
        title = "Renovar las caducadas" .. (due > 0 and (" (" .. due .. ")") or ""),
        disabled = due == 0,
        fn = function() M.login({ "--expired" }, "las caducadas") end,
    })
    table.insert(items, { title = "Renovar todas", fn = function() M.login({ "--all" }, "todas las cuentas") end })
    table.insert(items, { title = "Comprobar ahora", fn = M.refresh })
    return items
end

M.menu:setMenu(menuItems)
M.render, M.items, M.overall = render, menuItems, overall  -- checks: hs -c 'return #claudeAccounts.items()'
render()

if M.timer then M.timer:stop() end
M.timer = hs.timer.doEvery(REFRESH_SECONDS, M.refresh)

if M.wake then M.wake:stop() end
M.wake = hs.caffeinate.watcher.new(function(event)
    if event == hs.caffeinate.watcher.systemDidWake or event == hs.caffeinate.watcher.screensDidUnlock then
        M.wakeTimer = hs.timer.doAfter(10, M.refresh)
    end
end)
M.wake:start()

M.refresh()
