#!/usr/bin/env python3
"""Validierungsskript der WAB "Terraform vs. Ansible".

Werkzeugneutrales Erfolgskriterium, fuer beide Messreihen identisch.
Liest ausschliesslich die vSphere-API, aendert nichts.

Modi:
  consistency  Abgleich inventory.yaml (slots[].ip) gegen die feste IP der
               jeweiligen Customization Specification. Zu Beginn jeder Messreihe.
  precheck     Vorbedingung vor jedem Zyklus: Zielordner enthaelt keine Objekte.
  validate     Pruefkriterien je VM, Polling bis alle bestanden oder Timeout:
               Name genau einmal im Ordner, poweredOn, Tools laufen,
               Slot-Adresse unter den gemeldeten IPv4-Adressen und keine
               Adresse aus 169.254.0.0/16, guest.hostName exakt gleich VM-Name.
               Die Kriterien stehen in Kausalreihenfolge (CRITERIA).

VM-Auswahl im Modus validate:
  --tool tf|an --count p   die ersten p Slots (aufsteigend nach ID), Name aus
                           naming.terraform_prefix bzw. naming.ansible_prefix
                           plus Slot-ID -- dieselbe Auswahl wie Terraform
                           (for_each) und das Ansible-Vor-Play.
  --vm NAME=SLOT           explizite Zuordnung, z. B. fuer eine Probe-VM.

Exit-Codes: 0 = bestanden, 1 = nicht bestanden,
            2 = Konfigurations-, Verbindungs-, Aufruf- oder interner Fehler,
            3 = Anmeldung abgelehnt (keine Wiederholung: Sperrrisiko).
Exit-Code 1 bedeutet ausschliesslich ein nicht erfuelltes Pruefkriterium.

Mit --json steht genau ein JSON-Dokument auf stdout (auch im Fehlerfall),
gedacht fuer den Wrapper. Ohne --json Textausgabe wie bisher.

Verbindung ueber VMWARE_HOST, VMWARE_USER, VMWARE_PASSWORD, VMWARE_PORT
(optional, Standard 443), VMWARE_VALIDATE_CERTS (false = keine Pruefung).
IP-Adressen werden nur mit --verbose ausgegeben.
"""
import argparse
import ipaddress
import json
import os
import re
import sys
import time
from pathlib import Path

import yaml
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

EXIT_OK, EXIT_FAIL, EXIT_ERROR, EXIT_AUTH = 0, 1, 2, 3
LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")
CRITERIA = ("exists", "power", "tools", "ip", "hostname")  # Kausalreihenfolge
PREFIX_KEYS = {"tf": "terraform_prefix", "an": "ansible_prefix"}


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- Inventar

def load_inventory(path):
    """Liest inventory.yaml -> (slots {id: (spec, ip)}, prefixes {tf|an: praefix}).

    BaseLoader liefert alle Skalare als Zeichenkette. Damit wird die
    YAML-1.1-Falle umgangen, in der ungequotetes 01..07 als Oktalzahl,
    08/09 aber als Zeichenkette gelesen wuerde.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.load(f, Loader=yaml.BaseLoader)
    except OSError as e:
        raise ConfigError(f"Inventar nicht lesbar: {e}")
    except yaml.YAMLError as e:
        raise ConfigError(f"Inventar ist kein gueltiges YAML: {e}")
    if not isinstance(data, dict):
        raise ConfigError("Inventar enthaelt keine Zuordnung auf oberster Ebene")
    return parse_slots(data.get("slots")), parse_prefixes(data.get("naming"))


def parse_slots(slots):
    if not isinstance(slots, list) or not slots:
        raise ConfigError("Schluessel 'slots' fehlt oder ist leer")
    result, specs, ips = {}, set(), set()
    for n, entry in enumerate(slots, 1):
        if not isinstance(entry, dict):
            raise ConfigError(f"slots[{n}] ist kein Eintrag mit id/spec/ip")
        sid, spec, ip = (str(entry.get(k) or "").strip() for k in ("id", "spec", "ip"))
        if not re.fullmatch(r"\d{2}", sid):
            raise ConfigError(f"slots[{n}].id muss zweistellig sein, gefunden: {sid!r}")
        if not spec:
            raise ConfigError(f"slots[{n}].spec fehlt")
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            raise ConfigError(f"slots[{n}].ip ist keine gueltige IPv4-Adresse")
        if sid in result or spec in specs or ip in ips:
            raise ConfigError(f"slots[{n}]: id, spec oder ip doppelt vergeben")
        result[sid] = (spec, ip)
        specs.add(spec)
        ips.add(ip)
    return result


def parse_prefixes(naming):
    """Praefixe aus naming; muessen auf '-' enden (Name = Praefix + Slot-ID)."""
    if not isinstance(naming, dict):
        raise ConfigError("Schluessel 'naming' fehlt")
    prefixes = {}
    for tool, key in PREFIX_KEYS.items():
        pre = str(naming.get(key) or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*-", pre):
            raise ConfigError(f"naming.{key} fehlt, ist nicht klein geschrieben "
                              f"oder endet nicht auf '-': {pre!r}")
        prefixes[tool] = pre
    if prefixes["tf"] == prefixes["an"]:
        raise ConfigError("naming: terraform_prefix und ansible_prefix sind gleich")
    return prefixes


# ---------------------------------------------------------------- vCenter

def connect():
    try:
        host = os.environ["VMWARE_HOST"]
        user = os.environ["VMWARE_USER"]
        pwd = os.environ["VMWARE_PASSWORD"]
    except KeyError as e:
        raise ConfigError(f"Umgebungsvariable {e} fehlt (secrets.env geladen?)")
    try:
        port = int(os.environ.get("VMWARE_PORT", "443"))
    except ValueError:
        raise ConfigError("VMWARE_PORT ist keine Zahl")
    validate = os.environ.get("VMWARE_VALIDATE_CERTS", "true").lower() not in ("false", "no", "0")
    return SmartConnect(host=host, user=user, pwd=pwd, port=port,
                        disableSslCertValidation=not validate)


def find_folder(content, name):
    """Genau ein VM-Ordner mit exakt diesem Namen, sonst Abbruch."""
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.Folder], True)
    try:
        hits = [f for f in view.view if f.name == name and "VirtualMachine" in f.childType]
    finally:
        view.Destroy()
    if len(hits) != 1:
        raise ConfigError(f"VM-Ordner {name!r}: {len(hits)} Treffer, erwartet genau 1")
    return hits[0]


# ---------------------------------------------------------------- Modi
# Jeder Modus liefert (exit_code, textzeilen, json_dokument).

def cmd_consistency(content, slots):
    mgr = content.customizationSpecManager
    ok_all, lines, rows = True, [], []
    for sid in sorted(slots):
        spec_name, expected = slots[sid]
        if not mgr.DoesCustomizationSpecExist(spec_name):
            ok, detail = False, "Spec nicht vorhanden"
        else:
            nics = mgr.GetCustomizationSpec(spec_name).spec.nicSettingMap or []
            if len(nics) != 1:
                ok, detail = False, f"{len(nics)} NIC-Zuordnungen, erwartet 1"
            elif not isinstance(nics[0].adapter.ip, vim.vm.customization.FixedIp):
                ok, detail = False, "keine feste Adresse in der Spec"
            elif nics[0].adapter.ip.ipAddress != expected:
                ok, detail = False, "Adresse weicht von inventory.yaml ab"
            else:
                ok, detail = True, "Adresse stimmt ueberein"
        ok_all &= ok
        lines.append(f"slot={sid};spec={spec_name};ergebnis={'OK' if ok else 'FAIL'};detail={detail}")
        rows.append({"slot": sid, "spec": spec_name, "ok": ok, "detail": detail})
    verdict = "OK" if ok_all else "FAIL"
    lines.append(f"ergebnis={verdict}")
    return (EXIT_OK if ok_all else EXIT_FAIL), lines, \
        {"modus": "consistency", "ergebnis": verdict, "slots": rows}


def cmd_precheck(folder):
    children = [(c.name, type(c).__name__) for c in folder.childEntity]
    lines = [f"objekt={n};typ={t}" for n, t in children]
    verdict = "OK" if not children else "FAIL"
    lines.append(f"ergebnis={verdict};objekte={len(children)}")
    return (EXIT_OK if not children else EXIT_FAIL), lines, \
        {"modus": "precheck", "ergebnis": verdict,
         "objekte": [{"name": n, "typ": t} for n, t in children]}


def check_vm(folder, name, expected_ip, verbose):
    """Liefert [(kriterium, ok, detail)] fuer eine VM, in Kausalreihenfolge."""
    matches = [e for e in folder.childEntity
               if isinstance(e, vim.VirtualMachine) and e.name == name]
    if len(matches) != 1:
        return [("exists", False, f"{len(matches)} Treffer")]
    vm = matches[0]
    checks = [("exists", True, "1 Treffer")]

    power = vm.runtime.powerState
    checks.append(("power", power == "poweredOn", power))

    g = vm.guest  # ein Abruf, danach lokale Auswertung
    tools = g.toolsRunningStatus
    checks.append(("tools", tools == "guestToolsRunning", tools))

    ipv4 = []
    for nic in g.net or []:
        if nic.ipConfig:
            for a in nic.ipConfig.ipAddress:
                try:
                    ipv4.append(ipaddress.IPv4Address(a.ipAddress))
                except ValueError:
                    pass  # IPv6, z. B. Link-local
    has_expected = ipaddress.IPv4Address(expected_ip) in ipv4
    has_ll = any(a in LINK_LOCAL for a in ipv4)
    if has_ll:
        detail = "169.254-Adresse gemeldet"
    elif has_expected:
        detail = "Slot-Adresse gemeldet"
    else:
        detail = f"Slot-Adresse fehlt ({len(ipv4)} IPv4 gemeldet)"
    if verbose:
        detail += f" [gemeldet: {', '.join(map(str, ipv4)) or '-'}]"
    checks.append(("ip", has_expected and not has_ll, detail))

    checks.append(("hostname", g.hostName == name, repr(g.hostName)))
    return checks


def cmd_validate(folder, targets, timeout, interval, verbose):
    start = time.monotonic()
    rounds = 0
    while True:
        rounds += 1
        results = {}
        for name, (sid, ip) in targets.items():
            try:
                results[name] = check_vm(folder, name, ip, verbose)
            except vmodl.fault.ManagedObjectNotFound:
                # VM zwischen Auflistung und Abfrage verschwunden: Befund der VM.
                # Alle anderen API-Fehler (z. B. abgelaufene Sitzung) sind keine
                # VM-Befunde und fuehren ueber main() zu Exit-Code 2.
                results[name] = [("exists", False, "waehrend der Abfrage verschwunden")]
        ok_all = all(ok for r in results.values() for _, ok, _ in r)
        elapsed = time.monotonic() - start
        if ok_all or elapsed >= timeout:
            break
        time.sleep(interval)

    lines, vms = [], []
    for name, (sid, _) in targets.items():
        r = results[name]
        ok_vm = all(ok for _, ok, _ in r)
        parts = ";".join(f"{k}={'OK' if ok else 'FAIL'}({d})" for k, ok, d in r)
        lines.append(f"vm={name};slot={sid};{parts};gesamt={'OK' if ok_vm else 'FAIL'}")
        vms.append({"vm": name, "slot": sid, "ok": ok_vm,
                    "kriterien": [{"name": k, "ok": ok, "detail": d} for k, ok, d in r]})
    verdict = "OK" if ok_all else "FAIL"
    lines.append(f"ergebnis={verdict};dauer_s={elapsed:.1f};abfragen={rounds}")
    return (EXIT_OK if ok_all else EXIT_FAIL), lines, \
        {"modus": "validate", "ergebnis": verdict, "dauer_s": round(elapsed, 3),
         "abfragen": rounds, "timeout_s": timeout, "intervall_s": interval, "vms": vms}


# ---------------------------------------------------------------- Aufruf

def build_targets(args, slots, prefixes):
    """{vm_name: (slot_id, soll_ip)} aus --tool/--count oder --vm NAME=SLOT."""
    if args.tool:
        ids = sorted(slots)  # zweistellige IDs: lexikografisch = numerisch
        if not 1 <= args.count <= len(ids):
            raise ConfigError(f"--count {args.count}: erlaubt 1 bis {len(ids)} (Slots im Inventar)")
        pairs = [(prefixes[args.tool] + sid, sid) for sid in ids[:args.count]]
    else:
        pairs = []
        for item in args.vm:
            name, sep, sid = item.partition("=")
            if not sep or not name or not sid:
                raise ConfigError(f"--vm erwartet NAME=SLOT, gefunden: {item!r}")
            pairs.append((name, sid))
    targets = {}
    for name, sid in pairs:
        if sid not in slots:
            raise ConfigError(f"Slot {sid!r} nicht in inventory.yaml")
        if name in targets:
            raise ConfigError(f"VM {name!r} doppelt angegeben")
        targets[name] = (sid, slots[sid][1])
    return targets


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # Standard: inventory.yaml eine Ebene ueber scripts/, unabhaengig vom Arbeitsverzeichnis
    p.add_argument("--inventory", default=str(Path(__file__).resolve().parent.parent / "inventory.yaml"))
    p.add_argument("--folder", default="wab-benchmark")
    p.add_argument("--json", action="store_true", help="ein JSON-Dokument auf stdout")
    sub = p.add_subparsers(dest="mode", required=True)
    sub.add_parser("consistency")
    sub.add_parser("precheck")
    v = sub.add_parser("validate")
    g = v.add_mutually_exclusive_group(required=True)
    g.add_argument("--tool", choices=sorted(PREFIX_KEYS), help="Werkzeug: tf oder an")
    g.add_argument("--vm", action="append", metavar="NAME=SLOT",
                   help="explizite Zuordnung, mehrfach angebbar (z. B. fuer die Probe-VM)")
    v.add_argument("--count", type=int, help="Stufe p, nur mit --tool")
    # Bewusst ohne Standardwerte: fuer beide Werkzeuge explizit und identisch zu setzen
    v.add_argument("--timeout", type=float, required=True, help="Sekunden")
    v.add_argument("--interval", type=float, required=True, help="Sekunden zwischen Abfragen")
    v.add_argument("--verbose", action="store_true", help="gemeldete IP-Adressen ausgeben")
    args = p.parse_args()
    if args.mode == "validate":
        if args.tool and args.count is None:
            p.error("--tool verlangt --count")
        if args.count is not None and not args.tool:
            p.error("--count nur zusammen mit --tool")
        if args.timeout <= 0 or args.interval <= 0:
            p.error("--timeout und --interval muessen > 0 sein")
    return args


def emit(args, code, lines, doc):
    if args.json:
        doc = dict(doc, exit=code)
        print(json.dumps(doc, ensure_ascii=False))
    else:
        for line in lines:
            print(line)
    return code


def fail(args, code, kind, message):
    print(f"FEHLER ({kind}): {message}", file=sys.stderr)
    return emit(args, code, [], {"modus": args.mode, "ergebnis": "FEHLER",
                                 "fehlerart": kind, "meldung": message})


def main():
    args = parse_args()
    si = None
    try:
        slots, prefixes = load_inventory(args.inventory)
        targets = build_targets(args, slots, prefixes) if args.mode == "validate" else None
        si = connect()
        content = si.RetrieveContent()
        if args.mode == "consistency":
            return emit(args, *cmd_consistency(content, slots))
        folder = find_folder(content, args.folder)
        if args.mode == "precheck":
            return emit(args, *cmd_precheck(folder))
        return emit(args, *cmd_validate(folder, targets, args.timeout, args.interval, args.verbose))
    except ConfigError as e:
        return fail(args, EXIT_ERROR, "konfiguration", str(e))
    except vim.fault.InvalidLogin as e:  # vor MethodFault: ist eine Unterklasse
        return fail(args, EXIT_AUTH, "anmeldung", getattr(e, "msg", "") or type(e).__name__)
    except (vmodl.MethodFault, OSError) as e:
        return fail(args, EXIT_ERROR, "verbindung", f"{type(e).__name__}: {getattr(e, 'msg', e)}")
    except Exception as e:  # nie als Exit-Code 1 (= Kriterium nicht erfuellt) enden
        return fail(args, EXIT_ERROR, "intern", f"{type(e).__name__}: {e}")
    finally:
        if si is not None:
            try:
                Disconnect(si)
            except Exception:
                pass  # tote Verbindung darf das Ergebnis nicht ueberdecken


if __name__ == "__main__":
    sys.exit(main())
