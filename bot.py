"""VerseVPS Discord bot.  Made by Verseedit.

  !register <username> <password>   your account (same login for the dashboard, SSH and every VPS you own)
  !deploy                           free plan for yourself (admins change it with !deployedit)
  !create @user <ram> <ssd> <cpu>   admin creates a VPS for a registered user - details go to THAT user's DM
  !deployedit <ram> <ssd> <cpu>     admin: edit what !deploy gives
  !manage [id]                      Start / Stop / Restart / Refresh / SSH / SSHX terminal / Dashboard
"""
import asyncio
import logging
import sys

import discord
from discord.ext import commands

import core as C

log = logging.getLogger("versevps.bot")
RED, GREEN, BLUE, YELLOW = 0xED4245, 0x57F287, 0x5865F2, 0xFEE75C

intents = discord.Intents.default()
intents.message_content = True  # needed for "!" commands - enable it in the Developer Portal
bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents, help_command=None,
                   allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False, replied_user=False))


class NotAdmin(commands.CheckFailure):
    pass


def admin_only():
    async def pred(ctx):
        if C.is_admin(ctx.author.id):
            return True
        raise NotAdmin()
    return commands.check(pred)


def emb(title, desc=None, color=BLUE):
    e = discord.Embed(title=title, description=desc, color=color)
    e.set_footer(text=C.CREDIT)
    return e


async def panel_url():
    return await asyncio.to_thread(C.panel_url)


def spec_line(row):
    return f"{C.fmt_mib(row['ram_mib'])} RAM • {C.fmt_mib(row['disk_mib'])} SSD • {C.fmt_cpu(row['cpu'])} CPU"


def plan_line(p):
    return f"{p['ram']} RAM • {p['disk']} SSD • {C.fmt_cpu(p['cpu'])} CPU • {p['per_user']} per user"


async def dm_ok(user, title, text):
    """Open the DM first so a closed-DM user is found out BEFORE anything is created."""
    try:
        await user.send(embed=emb(title, text, YELLOW))
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def ssh_text(info):
    return (f"**Host / IP:** `{info['host']}`\n**Port:** `{info['port']}`\n**Username:** `root`\n"
            f"**Password:** the password you chose in `!register`\n**Command:** `{info['cmd']}`")


async def send_vps_info(user, row, headline="🖥️ Your VPS is ready"):
    """Everything in the owner's DM: SSH (ip, port, user, password) + dashboard login."""
    acct = await asyncio.to_thread(C.get_account, row["discord_id"])
    info = await asyncio.to_thread(C.ssh_info, row)
    url = await panel_url()
    e = emb(headline, f"Your VPS **root@{C.VPS_HOSTNAME}** is online. For safety the bot never stores or shows your "
                      "password - use the one you chose in `!register` (`!changepass <new>` changes it everywhere).", GREEN)
    e.add_field(name="VPS ID", value=f"`{row['id']}`")
    e.add_field(name="Specs", value=spec_line(row), inline=False)
    if info:
        e.add_field(name="🔑 SSH (root)", value=ssh_text(info), inline=False)
    e.add_field(name="🌐 Dashboard", value=f"{url}\n**Username:** `{acct['username']}`\n"
                                          "**Password:** same password", inline=False)
    e.add_field(name="OS", value=row["os"])
    e.add_field(name="Mode", value=row["backend"])
    await user.send(embed=e)


async def send_sshx(user, row):
    """Best effort: start the sshx.io web terminal and DM the link."""
    try:
        link = await asyncio.to_thread(C.sshx_link, row)
    except Exception as exc:  # noqa: BLE001
        log.warning("sshx failed for %s: %s", row["id"], exc)
        text = f"{str(exc)[:300]}\nTry again with `!manage {row['id']}` → **SSHX Terminal**."
        return await user.send(embed=emb("⚠️ SSHX terminal not started", text, YELLOW))
    await user.send(embed=emb("🌐 SSHX web terminal", f"{link}\n\nOpen it in any browser - you are root in `{row['id']}`. "
                                                     "Anyone with this link gets root, keep it private.", GREEN))


def stats_embed(row, s, note=None):
    e = emb(f"🖥️ VPS {row['id']}", note, GREEN if s["status"] == "Running" else RED)
    e.add_field(name="Status", value=s["status"])
    e.add_field(name="CPU", value=f"{s['cpu_pct']}% of {C.fmt_cpu(row['cpu'])} vCPU")
    e.add_field(name="RAM", value=f"{C.fmt_bytes(s.get('mem_used'))} / {C.fmt_mib(row['ram_mib'])}")
    e.add_field(name="SSD", value=f"{C.fmt_bytes(s.get('disk_used'))} / {C.fmt_mib(row['disk_mib'])}")
    e.add_field(name="Uptime", value=C.fmt_uptime(s.get("uptime")))
    e.add_field(name="Host name", value=f"root@{C.VPS_HOSTNAME}" if row["backend"] != "proc" else row["container"])
    e.add_field(name="OS", value=row["os"])
    e.add_field(name="Mode", value=row["backend"])
    if row.get("ssh_port"):
        e.add_field(name="SSH port", value=f"`{row['ssh_port']}`")
    return e


async def find_vps(ctx, vid):
    """Resolve '!cmd [id]' for the invoker; returns a row or None (after telling the user)."""
    if vid:
        row = await asyncio.to_thread(C.by_id, vid.strip().lower())
        if not row or not C.can_manage(ctx.author.id, row):
            await ctx.reply(embed=emb("❌ Not found", "No VPS with that ID that you can manage.", RED))
            return None
        return row
    mine = await asyncio.to_thread(C.for_user, ctx.author.id)
    if not mine:
        await ctx.reply(embed=emb("You have no VPS", "Use `!register` and then `!deploy` to get a free one.", YELLOW))
        return None
    return mine[0]


async def hide_secret_message(ctx):
    """!register / !changepass contain a password: delete the message at once. Returns True if it was in a server."""
    if ctx.guild is None:
        return False
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass
    return True


# ------------------------------------------------------------------ events
@bot.event
async def on_ready():
    C.write_status(state="online", bot=str(bot.user), bot_id=bot.user.id)
    await bot.change_presence(activity=discord.Game(f"!help • {C.CREDIT}"))
    log.info("ONLINE as %s (%s)", bot.user, bot.user.id)


@bot.event
async def on_command_error(ctx, error):
    error = getattr(error, "original", error)
    if isinstance(error, commands.CommandNotFound):
        return
    # NoPrivateMessage IS a CheckFailure - it must be tested first or DMs show "access denied"
    if isinstance(error, commands.NoPrivateMessage):
        msg = "This command only works inside a server, not in DMs."
    elif isinstance(error, NotAdmin):
        msg = "Only bot administrators can use this command."
    elif isinstance(error, commands.CheckFailure):
        msg = "You can't use this command here."
    elif isinstance(error, commands.MissingRequiredArgument):
        msg = f"Missing `{error.param.name}`.\nUsage: `!{ctx.command.qualified_name} {ctx.command.signature}`"
    elif isinstance(error, commands.UserInputError):
        msg = f"I couldn't understand that.\nUsage: `!{ctx.command.qualified_name} {ctx.command.signature}`"
    elif isinstance(error, (C.VPSError, ValueError)):
        msg = str(error)[:500]
    else:
        log.exception("command error in %s", ctx.command, exc_info=error)
        msg = f"Unexpected error: {type(error).__name__}: {str(error)[:300]}"
    try:
        await ctx.reply(embed=emb("❌ " + ("Busy" if isinstance(error, C.Busy) else "Error"), msg, RED))
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------- user cmds
@bot.command(name="help")
async def help_cmd(ctx):
    p = await asyncio.to_thread(C.deploy_plan)
    e = emb("🖥️ VerseVPS", f"Free root VPS from this bot. **{C.CREDIT}**")
    e.add_field(name="For everyone", inline=False, value=(
        "`!register <username> <password>` - your account (**do it in a DM with me**). Same login for dashboard, "
        "SSH and all your VPS\n"
        f"`!deploy` - free VPS with root ({p['ram']} RAM, {p['disk']} SSD, {C.fmt_cpu(p['cpu'])} CPU)\n"
        "`!myvps` - list your VPS\n`!manage [id]` - Start / Stop / Restart / Stats / SSH / SSHX terminal\n"
        "`!changepass <new password>` - new password everywhere (DM me)\n`!reinstall <id>` - wipe your VPS\n"
        "`!about` - info"))
    e.add_field(name="Admins", inline=False, value=(
        "`!create @user <ram> <ssd> <cpu> [os]` - e.g. `!create @bob 4GB 40GB 2`\n"
        "`!deployedit <ram> <ssd> <cpu> [per_user]` - edit the `!deploy` plan\n"
        "`!vpsdeploy on|off|status` - allow / block `!deploy`\n"
        "`!listall` `!delete <id>` `!status` `!panelurl [url|clear]`\n"
        "`!useraccess @user` `!revokeaccess @user` - add / remove admins"))
    await ctx.reply(embed=e)


@bot.command(name="about")
async def about(ctx):
    info = C.backend_info()
    e = emb("VerseVPS", f"VPS hosting bot + web dashboard.\n**{C.CREDIT}** ❤️", BLUE)
    e.add_field(name="Mode", value=info["label"], inline=False)
    e.add_field(name="Dashboard", value=await panel_url(), inline=False)
    await ctx.reply(embed=e)


@bot.command(name="register")
async def register(ctx, username: str, *, password: str):
    in_server = await hide_secret_message(ctx)
    acct = await asyncio.to_thread(C.register, ctx.author.id, username, password)
    url = await panel_url()
    text = (f"Account **{acct['username']}** created.\nThis username + password now work for the dashboard "
            f"({url}), SSH, and every VPS you create. Next: `!deploy`.")
    if in_server:
        text += ("\n\n⚠️ You typed the password in a server channel - I deleted the message, but change it now in a DM "
                 "with me: `!changepass <new password>`. Use DMs for `!register` next time.")
    try:
        await ctx.author.send(embed=emb("✅ Registered", text, GREEN))
        if in_server:
            await ctx.send(embed=emb("✅ Registered", f"{ctx.author.mention} account created - check your DM.", GREEN))
    except discord.HTTPException:
        await ctx.send(embed=emb("✅ Registered", text[:900], GREEN))
    if not in_server:
        pass


@bot.command(name="changepass")
async def changepass(ctx, *, new_password: str):
    in_server = await hide_secret_message(ctx)
    pending = await asyncio.to_thread(C.change_password, ctx.author.id, new_password)
    text = "Password changed for the dashboard and for the root login of all your VPS."
    if pending:
        text += f"\nStopped VPS ({', '.join(pending)}) get it the next time you start them."
    if in_server:
        text += "\n⚠️ Use DMs for this command so nobody can see the password."
    try:
        await ctx.author.send(embed=emb("🔑 Password changed", text, GREEN))
        if in_server:
            await ctx.send(embed=emb("✅ Done", f"{ctx.author.mention} password changed.", GREEN))
    except discord.HTTPException:
        await ctx.send(embed=emb("🔑 Password changed", text[:900], GREEN))


@bot.command(name="deploy")
async def deploy(ctx):
    user = ctx.author
    await asyncio.to_thread(C.deploy_check, user.id)  # not registered / off / limit / host full -> error handler
    plan = await asyncio.to_thread(C.deploy_plan)
    if not await dm_ok(user, "⏳ Deploying your VPS",
                       f"{plan['ram']} RAM • {plan['disk']} SSD • {C.fmt_cpu(plan['cpu'])} CPU\n"
                       "SSH + dashboard login arrive here in a moment."):
        return await ctx.reply(embed=emb("❌ I can't DM you", "Open your DMs (Server settings → Privacy → Direct "
                                         "Messages) and run `!deploy` again.", RED))
    status = await ctx.reply(embed=emb("⏳ Deploying…", "Installing SSH and root access - this can take a few minutes.", YELLOW))
    try:
        row = await asyncio.to_thread(C.deploy_vps, user.id)
    except (C.VPSError, ValueError) as exc:
        return await status.edit(embed=emb("❌ Deploy failed", str(exc)[:500], RED))
    except Exception as exc:  # noqa: BLE001
        log.exception("deploy failed")
        return await status.edit(embed=emb("❌ Deploy failed", f"{type(exc).__name__}: {str(exc)[:400]}", RED))
    try:
        await send_vps_info(user, row)
    except discord.HTTPException:
        return await status.edit(embed=emb("⚠️ VPS created, DM failed",
                                           f"Open your DMs, then run `!manage {row['id']}` → SSH.", YELLOW))
    await status.edit(embed=emb("✅ VPS deployed", f"{user.mention} SSH + dashboard details were sent to your DM.", GREEN))
    try:
        await send_sshx(user, row)
    except discord.HTTPException:
        pass


@bot.command(name="myvps")
async def myvps(ctx):
    mine = await asyncio.to_thread(C.for_user, ctx.author.id)
    if not mine:
        return await ctx.reply(embed=emb("You have no VPS", "Use `!register` and then `!deploy` to get a free one.", YELLOW))
    e = emb("Your VPS", None, BLUE)
    for r in mine:
        extra = f"\nSSH port `{r['ssh_port']}`" if r.get("ssh_port") else ""
        e.add_field(name=f"`{r['id']}`", value=spec_line(r) + f"\n{r['os']} • {r['backend']}{extra}", inline=False)
    await ctx.reply(embed=e)


class ManageView(discord.ui.View):
    def __init__(self, invoker_id, vid, url):
        super().__init__(timeout=300)
        self.invoker_id, self.vid, self.message = invoker_id, vid, None
        for label, style, action in (("Start", discord.ButtonStyle.success, "start"),
                                     ("Stop", discord.ButtonStyle.danger, "stop"),
                                     ("Restart", discord.ButtonStyle.primary, "restart"),
                                     ("Refresh", discord.ButtonStyle.secondary, "stats")):
            b = discord.ui.Button(label=label, style=style, row=0)
            b.callback = self._make(action)
            self.add_item(b)
        for label, cb in (("🔑 SSH", self._ssh), ("🌐 SSHX Terminal", self._sshx)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=1)
            b.callback = cb
            self.add_item(b)
        self.add_item(discord.ui.Button(label="Dashboard", style=discord.ButtonStyle.link, url=url, row=1))

    def _make(self, action):
        async def cb(inter):
            await self._handle(inter, action)
        return cb

    async def interaction_check(self, inter):
        if inter.user.id != self.invoker_id:
            await inter.response.send_message("This panel belongs to someone else - run `!manage` yourself.", ephemeral=True)
            return False
        return True

    def _disable(self, flag):
        for child in self.children:
            if isinstance(child, discord.ui.Button) and not child.url:
                child.disabled = flag

    async def _row(self, inter):
        row = await asyncio.to_thread(C.by_id, self.vid)
        if not row or not C.can_manage(inter.user.id, row):
            await inter.response.edit_message(embed=emb("VPS not available", color=RED), view=None)
            return None
        return row

    async def _ssh(self, inter):
        row = await self._row(inter)
        if not row:
            return
        await inter.response.defer(ephemeral=True)
        info = await asyncio.to_thread(C.ssh_info, row)
        if not info:
            return await inter.followup.send(embed=emb("No SSH", "This VPS mode has no SSH access.", YELLOW), ephemeral=True)
        await inter.followup.send(embed=emb(f"🔑 SSH - {row['id']}", ssh_text(info), GREEN), ephemeral=True)

    async def _sshx(self, inter):
        row = await self._row(inter)
        if not row:
            return
        await inter.response.defer(ephemeral=True)
        try:
            link = await asyncio.to_thread(C.sshx_link, row)
        except Exception as exc:  # noqa: BLE001
            return await inter.followup.send(embed=emb("⚠️ SSHX failed", str(exc)[:500], RED), ephemeral=True)
        await inter.followup.send(embed=emb("🌐 SSHX web terminal", f"{link}\nAnyone with this link is root in the VPS.",
                                            GREEN), ephemeral=True)

    async def _handle(self, inter, action):
        row = await self._row(inter)
        if not row:
            return
        await inter.response.defer()
        note = None
        if action != "stats":
            self._disable(True)
            await inter.edit_original_response(embed=emb(f"⏳ {action}…", color=YELLOW), view=self)
            try:
                await asyncio.to_thread(C.power, row, action)
                note = f"✅ {action} done"
            except C.Busy as exc:  # same lock the web dashboard uses
                note = f"⚠️ {exc}"
            except Exception as exc:  # noqa: BLE001
                log.exception("power %s failed", action)
                note = f"❌ {str(exc)[:200]}"
            self._disable(False)
        s = await asyncio.to_thread(C.stats, row)
        await inter.edit_original_response(embed=stats_embed(row, s, note), view=self)

    async def on_timeout(self):
        self._disable(True)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.command(name="manage")
async def manage(ctx, vid: str = None):
    row = await find_vps(ctx, vid)
    if not row:
        return
    s = await asyncio.to_thread(C.stats, row)
    view = ManageView(ctx.author.id, row["id"], await panel_url())
    view.message = await ctx.reply(embed=stats_embed(row, s), view=view)


class ConfirmView(discord.ui.View):
    def __init__(self, invoker_id):
        super().__init__(timeout=60)
        self.invoker_id, self.value = invoker_id, None

    async def interaction_check(self, inter):
        if inter.user.id != self.invoker_id:
            await inter.response.send_message("Not your confirmation.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, wipe it", style=discord.ButtonStyle.danger)
    async def yes(self, inter, button):
        self.value = True
        await inter.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, inter, button):
        self.value = False
        await inter.response.defer()
        self.stop()


@bot.command(name="reinstall")
async def reinstall(ctx, vid: str):
    row = await find_vps(ctx, vid)
    if not row:
        return
    view = ConfirmView(ctx.author.id)
    msg = await ctx.reply(embed=emb("⚠️ Reinstall?", f"This **wipes all data** on `{row['id']}`. Login and SSH port stay the same.", YELLOW), view=view)
    await view.wait()
    if not view.value:
        return await msg.edit(embed=emb("Cancelled", color=BLUE), view=None)
    await msg.edit(embed=emb("⏳ Reinstalling…", "Installing SSH again, a few minutes.", YELLOW), view=None)
    try:
        await asyncio.to_thread(C.reinstall, row)
    except Exception as exc:  # noqa: BLE001
        log.exception("reinstall failed")
        return await msg.edit(embed=emb("❌ Reinstall failed", str(exc)[:400], RED))
    await msg.edit(embed=emb("✅ Reinstalled", f"`{row['id']}` is fresh again.", GREEN))


# --------------------------------------------------------------- admin cmds
@bot.command(name="create")
@commands.guild_only()
@admin_only()
async def create(ctx, user: discord.User, ram: str, ssd: str, cpu: str, os_name: str = None):
    """!create @user 4GB 40GB 2 [os]  - the VPS belongs to the TAGGED user and the details go to THEIR DM."""
    if user.bot:
        return await ctx.reply(embed=emb("❌ Bots can't own a VPS", color=RED))
    if not await asyncio.to_thread(C.get_account, user.id):
        return await ctx.reply(embed=emb("❌ Not registered", f"{user.mention} must run `!register <username> <password>` "
                                         "(in a DM with me) first.", RED))
    await asyncio.to_thread(C.ensure_root_backend)
    await asyncio.to_thread(C.parse_spec, ram, ssd, cpu, os_name)  # bad size / host full -> error handler, no DM sent
    spec_txt = f"{ram} RAM • {ssd} SSD • {cpu} CPU"
    if not await dm_ok(user, "⏳ A VPS is being created for you", f"{spec_txt}\nSSH + dashboard login arrive here in a moment."):
        return await ctx.reply(embed=emb("❌ Can't DM that user", f"{user.mention} must allow DMs from server members first.", RED))
    status = await ctx.reply(embed=emb("⏳ Creating VPS…", f"For {user.mention}: {spec_txt}", YELLOW))
    try:
        row = await asyncio.to_thread(C.create_vps, user.id, ram, ssd, cpu, os_name, "admin")
    except (C.VPSError, ValueError) as exc:
        await status.edit(embed=emb("❌ Creation failed", str(exc)[:500], RED))
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("create failed")
        await status.edit(embed=emb("❌ Creation failed", f"{type(exc).__name__}: {str(exc)[:400]}", RED))
        return
    try:
        await send_vps_info(user, row)
    except discord.HTTPException:
        return await status.edit(embed=emb("⚠️ Created, but the DM failed",
                                           f"Ask {user.mention} to open DMs, then run `!manage {row['id']}`.", YELLOW))
    await status.edit(embed=emb("✅ VPS created", f"{user.mention} got SSH + dashboard details in DM.\n"
                                                  f"ID `{row['id']}` • {spec_line(row)}", GREEN))
    try:
        await send_sshx(user, row)
    except discord.HTTPException:
        pass


@bot.command(name="deployedit")
@commands.guild_only()
@admin_only()
async def deployedit(ctx, ram: str, storage: str, cpu: str, per_user: int = None):
    """!deployedit 4GB 20GB 1 [per_user]  - change what !deploy gives (applies to new deploys)."""
    plan = await asyncio.to_thread(C.set_deploy_plan, ram, storage, cpu, per_user)
    await ctx.reply(embed=emb("✅ `!deploy` plan updated", plan_line(plan), GREEN))


@bot.command(name="vpsdeploy")
@commands.guild_only()
@admin_only()
async def vpsdeploy(ctx, state: str = "status"):
    state = state.lower()
    if state in ("on", "off"):
        await asyncio.to_thread(C.set_deploy_enabled, state == "on")
    elif state != "status":
        raise commands.BadArgument()
    on = await asyncio.to_thread(C.deploy_enabled)
    plan = await asyncio.to_thread(C.deploy_plan)
    await ctx.reply(embed=emb("`!deploy` is " + ("ON ✅" if on else "OFF ⛔"),
                              f"Plan: {plan_line(plan)}\nChange it with `!deployedit <ram> <ssd> <cpu> [per_user]`.",
                              GREEN if on else RED))


@bot.command(name="delete")
@commands.guild_only()
@admin_only()
async def delete(ctx, vid: str):
    row = await asyncio.to_thread(C.by_id, vid.strip().lower())
    if not row:
        return await ctx.reply(embed=emb("❌ Not found", color=RED))
    await asyncio.to_thread(C.delete_vps, row)
    await ctx.reply(embed=emb("🗑️ Deleted", f"`{row['id']}` (owner <@{row['discord_id']}>) is gone.", GREEN))


@bot.command(name="listall")
@commands.guild_only()
@admin_only()
async def listall(ctx):
    rows = await asyncio.to_thread(C.all_vps)
    if not rows:
        return await ctx.reply(embed=emb("No VPS yet", color=YELLOW))
    lines = [f"`{r['id']}` <@{r['discord_id']}> • {spec_line(r)} • {r['source']}" for r in rows]
    text, shown = "", 0
    for ln in lines:
        if len(text) + len(ln) > 3800:
            break
        text += ln + "\n"
        shown += 1
    if shown < len(lines):
        text += f"…and {len(lines) - shown} more"
    await ctx.reply(embed=emb(f"All VPS ({len(rows)})", text, BLUE))


@bot.command(name="status")
@commands.guild_only()
@admin_only()
async def status_cmd(ctx):
    cap = await asyncio.to_thread(C.capacity)
    info = C.backend_info()
    on = await asyncio.to_thread(C.deploy_enabled)
    e = emb("📊 Host status", None, BLUE)
    e.add_field(name="Mode", value=info["label"], inline=False)
    e.add_field(name="RAM", value=f"{C.fmt_mib(cap['used_ram'])} / {C.fmt_mib(cap['max_ram'])} used")
    e.add_field(name="SSD", value=f"{C.fmt_mib(cap['used_disk'])} / {C.fmt_mib(cap['max_disk'])} used")
    e.add_field(name="CPU", value=f"{C.fmt_cpu(round(cap['used_cpu'], 2))} / {C.fmt_cpu(cap['max_cpu'])} used")
    e.add_field(name="VPS", value=str(cap["count"]))
    e.add_field(name="!deploy", value="ON" if on else "OFF")
    e.add_field(name="Dashboard", value=await panel_url(), inline=False)
    if C.REQUIRE_ROOT and not C.get_backend(C.ACTIVE).has_root:
        e.add_field(name="⛔ No root", inline=False,
                    value="This host has no Docker/LXD, so VPS creation is blocked (users must get root). "
                          "Install Docker: `apt install docker.io`, then restart the bot.")
    if info["notes"]:
        e.add_field(name="Notes", value="\n".join("• " + n for n in info["notes"])[:1000], inline=False)
    await ctx.reply(embed=e)


@bot.command(name="panelurl")
@commands.guild_only()
@admin_only()
async def panelurl(ctx, url: str = None):
    if url is None:
        return await ctx.reply(embed=emb("Dashboard URL", await panel_url(), BLUE))
    if url.lower() == "clear":
        await asyncio.to_thread(C.set_setting, "panel_url", None)
    elif url.startswith(("http://", "https://")):
        await asyncio.to_thread(C.set_setting, "panel_url", url.rstrip("/"))
    else:
        return await ctx.reply(embed=emb("❌ Invalid URL", "It must start with http:// or https://", RED))
    await ctx.reply(embed=emb("✅ Dashboard URL", await panel_url(), GREEN))


@bot.command(name="useraccess")
@commands.guild_only()
@admin_only()
async def useraccess(ctx, user: discord.User):
    added = await asyncio.to_thread(C.add_admin, user.id)
    await ctx.reply(embed=emb("✅ Admin added" if added else "Already an admin", user.mention, GREEN if added else YELLOW))


@bot.command(name="revokeaccess")
@commands.guild_only()
@admin_only()
async def revokeaccess(ctx, user: discord.User):
    if user.id in C.ENV_ADMINS:
        return await ctx.reply(embed=emb("❌ Owner admin", "Admins from ADMIN_IDS in .env can't be removed here.", RED))
    gone = await asyncio.to_thread(C.remove_admin, user.id)
    await ctx.reply(embed=emb("✅ Removed" if gone else "Was not an admin", user.mention, GREEN if gone else YELLOW))


def run(token):
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        C.write_status(state="bad_token")
        log.error("Discord rejected the bot token. Run the installer again with RECONFIGURE=1.")
        sys.exit(2)
    except discord.PrivilegedIntentsRequired:
        C.write_status(state="intents")
        log.error("Enable 'MESSAGE CONTENT INTENT' in the Discord Developer Portal (Bot tab), then restart.")
        sys.exit(3)
