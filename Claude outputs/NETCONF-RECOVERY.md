# Recovering NETCONF on the KSwitch D10 MMT after GA-3.06

**Subject:** why the NETCONF server stopped starting on all five Kontron
KSwitch D10 MMT switches after the GA-3.06 firmware update, and how to bring
it back.

**Hardware:** 5 × Kontron KSwitch D10 MMT, `192.168.1.10`–`.14`
(`KSwitchTSN-1` … `-5`), firmware **GA-3.06**, build `20260812131346`.

**Status:** root cause identified and fixed on SW1 (`192.168.1.10`), verified
end to end from UP-1. SW2–SW5 pending, same procedure.

**Date:** 2026-09-18

---

## 1. Summary

After the GA-3.06 update the NETCONF server did not start on any of the five
switches. `netconf server` was present in every running configuration, port
830 was refused on every switch, and no `netopeer2-server` or
`sysrepo-plugind` process existed.

The cause is not NETCONF, not the configuration, and not the credentials. It
is **six lines of shell**:

```sh
# /usr/sbin/netopeer2_config.sh
CONFIG_DONE="/switch/netopeer2_config.done"

if [[ -f ${CONFIG_DONE} ]]; then
	exit 0
fi
```

That marker file lives on the persistent partition and survives a firmware
upgrade. GA-3.06 ships new `libyang`, `sysrepo` and `netopeer2` binaries, but
because the marker was written on **27 June 2025** by the previous release,
provisioning exits 0 and never runs again. The sysrepo YANG repository left
behind by the old release is then read by the new libraries, which cannot
resolve it, and every sysrepo client — `sysrepoctl`, `sysrepocfg`,
`sysrepo-plugind`, `netopeer2-server` — dies at connect with:

```
[ERR] Loading "yang" module failed, not found.
```

There is no version check anywhere in that path.

**The fix is two commands and a reboot per switch.** A factory-fresh unit is
unaffected, which is presumably why this shipped.

---

## 2. Symptom

From the client (UP-1):

```
$ nc -vz 192.168.1.10 830
nc: connect to 192.168.1.10 port 830 (tcp) failed: Connection refused
```

From the switch CLI:

```
KSwitchTSN-1# show running-config | include netconf
netconf server                     <- configured
```

From the switch's Linux shell:

```
~ # ps | grep -E 'netopeer|sysrepo'
(nothing)
~ # netstat -ltn | grep 830
(nothing)
```

Configured, not running, nothing listening. **Connection refused is a TCP
reset, not a timeout**, so no firewall or access-management rule is involved —
there is genuinely no listener.

Reproduced identically on all five switches, which is what makes it a
firmware fault rather than a per-device mistake.

---

## 3. The diagnostic trail

Recorded in the order it actually went, because the order is the useful part.

### 3.1 The binaries exist

```
~ # which netopeer2-server sysrepo-plugind sysrepoctl sysrepocfg
/usr/sbin/netopeer2-server
/usr/bin/sysrepo-plugind
/usr/bin/sysrepoctl
/usr/bin/sysrepocfg
```

So the image was not built without NETCONF support.

### 3.2 Everything fails at *connect*, before any NETCONF work

```
~ # sysrepocfg --xpath "/ietf-system:system/*" -X
[ERR] Loading "yang" module failed, not found.
sysrepocfg error: Failed to connect (libyang error)

~ # netopeer2-server -d -v3
[ERR]: SR: Loading "yang" module failed, not found.
[ERR]: NP: Connecting to sysrepo failed (libyang error).
[ERR]: NP: Server init failed.
```

This is the single most informative step. `sysrepocfg` is not netopeer2 and
does not care about NETCONF — it only talks to sysrepo. Its failing the same
way moves the problem below NETCONF entirely, to the sysrepo/libyang layer.

Run it early. It is the cheapest test in this whole sequence.

### 3.3 The repository is there, and it is old

```
~ # ls -la /etc/sysrepo/
drwxrwxrwx  conn           Aug 12 19:28
drwxrwxrwx  data           Jun 27  2025
-rw-rw-rw-  sr_main_lock   Jun 27  2025
drwxrwxrwx  yang           Jun 27  2025

~ # ls /etc/sysrepo/yang/ | wc -l
53
```

53 schema files and a populated datastore, all dated **June 2025**, while the
binaries are dated **August 2026** and the firmware build ID is
`GA-3.06-20260812131346`. The repository predates the firmware by more than a
year.

> **A dead end worth recording.** The absence of a `yang@*.yang` file in that
> directory is *not* the problem. `yang`, `ietf-yang-metadata`,
> `ietf-inet-types` and `ietf-yang-types` are compiled into libyang and are
> never written to the repository. Their absence is normal.

### 3.4 Why it survived the upgrade

```
~ # grep -i overlay /proc/mounts
overlay1 /etc overlay rw,lowerdir=/etc,upperdir=/switch/etc/upper,workdir=/switch/etc/work
```

`/etc` is mounted **over itself**: the image's `/etc` on the read-only
squashfs is the lower layer, and a persistent copy on `/dev/mmcblk0p3` is
layered on top. The upgrade replaced the image; it did not touch the
persistent upper layer.

And the image's own copy is empty:

```
~ # mkdir -p /switch/rootview && mount -o bind / /switch/rootview
~ # ls -la /switch/rootview/etc/sysrepo/
total 8
drwxrwxrwx  .   Aug 12 14:34
drwxr-xr-x  ..  Aug 12 14:52
```

A bind mount of `/` does not carry the submount on `/etc`, so this is the
only way to see what the image actually ships: an empty directory. The
repository is not shipped — it is **built once, at provisioning time**.

### 3.5 Who builds it, and what stops it

```
~ # grep -rl netopeer2_config /etc /usr /sbin /bin 2>/dev/null
/usr/bin/switch_app
```

The ISTAX application itself. And the scripts it calls are in the image,
readable:

```
/usr/sbin/netopeer2_config.sh              ← entry point, marker guard
/usr/sbin/netopeer2_module_setup.sh        ← installs the YANG modules
/usr/sbin/netopeer2_create_hostkey.sh
/usr/sbin/netopeer2_netconf_server_config.sh
/usr/sbin/netopeer2_nacm_config.sh
/usr/sbin/netopeer2_config_clear.sh        ← and an undo
```

with the schemas beside them:

```
/usr/share/yang/modules/{kontron-plugin,libyang,libnetconf2,netopeer2,sysrepo}/
```

`kontron-plugin/` holds the 14 switch models — `ieee802-dot1q-bridge`,
`-sched` (Qbv), `-preemption` (Qbu), `dot1ab-lldp`, `ietf-interfaces`,
`ietf-system`, `ietf-ip`, `ietf-routing`. Everything needed to rebuild is on
the box.

The entry point is quoted in §1. It is guarded by a marker in `/switch`, the
persistent partition, and there is no version comparison of any kind.

### 3.6 Confirmation

Moving the stale repository aside and letting sysrepo recreate it produced a
working connection immediately, and named the mismatch exactly:

```
~ # sysrepoctl -l
Module Name  | Revision   | Flags | ...
yang         | 2025-01-29 | I     | ...
```

`yang` revision **2025-01-29** is what the GA-3.06 libyang carries
internally. The June 2025 repository recorded a different revision, had no
`yang@<that revision>.yang` file to fall back to, and libyang reported "not
found".

---

## 4. Root cause

**`/usr/sbin/netopeer2_config.sh` guards NETCONF provisioning on
`/switch/netopeer2_config.done`, a marker in persistent storage that survives
firmware upgrade, with no check of the sysrepo or libyang version that wrote
the repository.**

On an upgraded unit:

1. GA-3.06 installs new `libyang` / `sysrepo` / `netopeer2` binaries.
2. `switch_app` calls `netopeer2_config.sh` at startup.
3. The marker from the previous release exists, so the script exits 0.
4. The repository under `/switch/etc/upper/sysrepo`, written by the previous
   release, is what the new libraries find.
5. Its recorded internal-module revisions do not match the new libyang.
6. Every sysrepo client fails at connect.
7. `netopeer2-server` therefore exits within milliseconds of starting, which
   is why no process and no listening socket are ever visible.

On a factory-fresh unit, provisioning runs once against its own libraries and
everything works. The fault is specific to the upgrade path.

---

## 5. The fix

Per switch. Nothing here touches switch configuration — VLANs, PTP and TAS
state live in the ISTAX application, not in sysrepo.

### 5.1 Open a shell

```
KSwitchTSN-1# platform debug allow
KSwitchTSN-1# debug system shell
```

### 5.2 Back up the old repository

Do **not** redirect stderr — the first attempt at this silently produced no
file because busybox `tar` here has no gzip:

```sh
tar cf /switch/sysrepo-backup.tar /etc/sysrepo ; echo "exit=$?"
ls -la /switch/sysrepo-backup.tar
```

### 5.3 Clear the marker and the stale repository

```sh
rm -f /switch/netopeer2_config.done
mv /etc/sysrepo /etc/sysrepo.stale
exit
```

### 5.4 Save and reboot

```
KSwitchTSN-1# copy running-config startup-config
KSwitchTSN-1# reload cold
```

Provisioning runs at boot, from a clean `/dev/shm`, with nothing else
connected to sysrepo. That is the same condition a factory-fresh unit
provisions under, and it is why this works where running the script by hand
does not (§6.3).

### 5.5 Verify

```sh
sysrepoctl -l | wc -l          # ~64 lines; was 17 module rows before
netstat -ltn | grep 830        # 0.0.0.0:830 LISTEN
cat /switch/netopeer2_config.done
```

and from the client:

```bash
nc -vz 192.168.1.10 830
cd ~/tsn-testbed/topology-discovery && ./scripts/preflight.sh
```

Result on SW1:

```
== 4. NETCONF port 830/tcp
  PASS  SW1  192.168.1.10  port 830 open
== 5. NETCONF session
  PASS  SW1  192.168.1.10  session=1 modules=17
        tsn=ieee802-dot1q-bridge,ieee802-dot1q-sched
```

---

## 6. What was *not* the cause

Recorded because each of these cost time, and each looks plausible.

**The `netconf server` configuration.** AN001 §4 says "The netconf server is
not started automatically. The netconf server has to be enabled first", which
sends you to `configure terminal` / `netconf server`. It was already present
in every running-config. The instruction is correct for a fresh unit and
actively misleading here.

**A firewall or access-management rule.** Port 830 returned `Connection
refused` — a TCP reset — not a timeout. Nothing was filtering; nothing was
listening.

**Credentials.** The `netconf` account works. AN001 v1.3 documents the
default as `netconf`/`netconf` while `SYSTEM.md` records `netconf`/`geheim`;
the latter authenticated successfully once port 830 opened. The earlier
authentication failures were from attempts against **port 22**, which is the
ISTAX management CLI and not a NETCONF transport.

**A reboot on its own.** The switch had already rebooted after the upgrade and
came up broken. `uptime` confirmed 4:45 at the time of diagnosis, so this had
been tested without anyone realising.

**Toggling `no netconf server` / `netconf server`.** Not tested, and it would
not have helped: the failure is below that layer, in sysrepo's connect path,
and toggling the configuration does not rebuild the repository.

---

## 7. Pitfalls

Things that will bite anyone repeating this.

### 7.1 Deleting through an overlay does the opposite of what you want

`/etc` is an overlay. Removing `/etc/sysrepo` through the merged mount writes
a **whiteout** in the upper layer, which permanently hides the image's copy.
Use `mv` to rename it aside instead, as in §5.3.

Related: after `mv /etc/sysrepo /etc/sysrepo.stale`, a `mkdir /etc/sysrepo`
fails with `File exists`. That is correct and is the signal it worked — the
image's empty directory is now showing through.

### 7.2 The provisioning script writes its marker even when it fails

`netopeer2_config.sh` has no error handling. `touch ${CONFIG_DONE}` runs
unconditionally after the four sub-scripts, so a failed provisioning run
still marks itself done. **After any failed attempt, remove the marker
again** before retrying.

### 7.3 Running the script by hand on a live system fails

```
[WRN] Recovered a read-lock of CID 439 (_sr_install_modules).
... ×24
[ERR] Waiting on a conditional variable failed (Connection timed out).
sysrepoctl error: Failed to install modules (Timeout expired)
#### ERROR: installing ietf-truststore
```

Module installation needs an exclusive lock. `switch_app` supervises
`sysrepo-plugind` and **respawns it** — killing it produces a new PID within
seconds — so on a running system there is always a client attached, plus the
debris of any manual debugging. Provision at boot instead.

### 7.4 `start-stop-daemon` reporting OK means nothing

```
~ # /etc/init.d/S52netopeer2-server start
Starting netopeer2-server: OK
~ # ps | grep netopeer
(nothing)
```

It reports that the fork succeeded. Always confirm with `ps` and `netstat`,
and get the real reason with `netopeer2-server -d -v3` in the foreground.

### 7.5 Changes under `/etc` persist; `/dev/shm` does not

The overlay's upper layer is on `/dev/mmcblk0p3`, so an edit under `/etc`
survives reboot — good for the fix, bad for a mistake. `/dev/shm` is tmpfs
and is cleared at boot, so never `rm /dev/shm/sr_*` while a process has it
mapped; just reboot.

### 7.6 `debug system shell` needs enabling first

```
KSwitchTSN-1# platform debug allow
KSwitchTSN-1# debug system shell
```

AN001 p. 33 documents this, with the warning that debug commands are
unsupported and subject to change.

---

## 8. Report to Kontron

> On KSwitch D10 MMT units **upgraded** to GA-3.06 (build `20260812131346`),
> the NETCONF server never starts. `netconf server` is present in
> running-config, TCP 830 is refused, and no `netopeer2-server` or
> `sysrepo-plugind` process exists. Reproduced on all five units in our
> testbed.
>
> **Root cause.** `/usr/sbin/netopeer2_config.sh` guards NETCONF provisioning
> on `/switch/netopeer2_config.done`, which lives on the persistent partition
> and survives firmware upgrade:
>
> ```sh
> CONFIG_DONE="/switch/netopeer2_config.done"
> if [[ -f ${CONFIG_DONE} ]]; then
> 	exit 0
> fi
> ```
>
> GA-3.06 ships an empty `/etc/sysrepo` in the image and new
> libyang/sysrepo/netopeer2 binaries, but because the marker is present
> (written 2025-06-27 by the previous release) provisioning is skipped and
> the repository persisted at `/switch/etc/upper/sysrepo` is used instead. It
> is incompatible: the new libyang's internal `yang` module is revision
> `2025-01-29`, the persisted repository records a different one, and
> `sysrepoctl`, `sysrepocfg`, `sysrepo-plugind` and `netopeer2-server` all
> fail at connect with `[ERR] Loading "yang" module failed, not found`.
>
> Factory-fresh units are unaffected, which is presumably why this escaped
> validation.
>
> **Suggested fix.** Compare the sysrepo/libyang version recorded in the
> repository against the installed one and re-provision on mismatch, or have
> the upgrade remove the marker. A secondary issue: `netopeer2_config.sh`
> writes its completion marker unconditionally, so a provisioning run that
> fails partway still marks itself done.

Worth sending regardless of the local workaround. A hand-fix under `/etc` on
five switches will be undone by the next upgrade in exactly the same way.

---

## 9. Consequences for the discovery subproject

**The NETCONF path becomes testable for the first time.** Until now no
NETCONF session had ever been established against a KSwitch D10, and
`REPORT.md` §9 said so. SW1 now answers, which means the record-uniformity
test finally has real NETCONF output to compare against instead of synthetic
fixtures.

**The CLI fallback is not wasted, and is not redundant.** It was specified as
a complementary transport rather than a replacement, and that is what it now
is: the probe picks NETCONF where it answers and the CLI where it does not,
which is exactly the mixed state during a rollout. More importantly, **the
PTP finding is unchanged** — no PTP or gPTP YANG module is implemented on
this hardware, so the CLI remains the only way to read the time base, and
`ptp.py` stays load-bearing.

**Two things to check on the first all-NETCONF run.** The freshly provisioned
repository advertises **17 modules**, not the eleven AN001 v1.2/v1.3
documents, so the capability table must be regenerated from a live run rather
than edited by hand. And `ieee802-dot1q-preemption` did not appear in
preflight's TSN summary despite its schema being present in
`kontron-plugin/` — if Qbu is genuinely not implemented over NETCONF, the CLI
remains the only source for preemption state, as it is for PTP.

**One documentation fix in this repo.** `scripts/preflight.sh` prints
*"port 830 closed or filtered — is the NETCONF server enabled? (ISTAX:
configure terminal / netconf server)"*. On GA-3.06 that is the wrong advice
and it cost two sessions. It should point at the provisioning marker instead.

---

## 10. Files touched on the switch

| Path | What | Reversible |
|---|---|---|
| `/switch/netopeer2_config.done` | provisioning marker, removed | rewritten by provisioning |
| `/etc/sysrepo` | stale repository, renamed to `.stale` | `mv` it back |
| `/switch/sysrepo-backup.tar` | backup taken before the change | `tar xf` |

Keep `/etc/sysrepo.stale` until a full discovery run over NETCONF succeeds,
then remove it and the tar.
