#!/usr/bin/env python3
"""Wrapper der WAB "Terraform vs. Ansible": orchestriert die Benchmark-Zyklen.

Liegt in scripts/ neben ansible/, terraform/ und inventory.yaml; Ergebnisse,
Logs und Laufdateien entstehen eine Ebene hoeher (results/, logs/, run/).
Start ueber scripts/start_wrapper.sh (laedt secrets.env, nutzt den Python des venv).

  wrapper.py plan --phase trocken|vorlauf|vorlauf32|haupt --runden N --seed S
                  [--stufen 1 2 4 8 16 32] [--werkzeuge tf an]
      Erzeugt den Ablaufplan run/plan_<phase>.json: je Runde jede Stufe einmal
      mit jedem Werkzeug, Stufenreihenfolge je Runde mit festem Seed
      randomisiert, startendes Werkzeug wechselt je Runde.
      Phase vorlauf32: nachgezogener Vorlauf nur fuer Stufe 32 (eigener Plan
      und eigene Ergebnisdateien, der Vorlauf der Stufen 1-16 bleibt unberuehrt),
      z. B. plan --phase vorlauf32 --runden 5 --seed S --stufen 32.

  wrapper.py run --phase trocken|vorlauf|vorlauf32|haupt
      Arbeitet den Plan ab. Bereits protokollierte Eintraege werden
      uebersprungen; nach einem Abbruch genuegt derselbe Aufruf.

  wrapper.py status
      Zeigt run/status.json und den Fortschritt je Phase.

Anhalten nach dem laufenden Zyklus: Datei run/STOP anlegen (touch run/STOP).

Aenderungen in 1.4 (nach dem Hauptlauf; Messablauf und Zeitmessung unveraendert):
  - Zyklen, die ungueltig werden und die Reihe anhalten (Anmeldung abgelehnt,
    Watchdog in Deploy oder Reset), werden wie alle anderen ungueltigen Zyklen
    zum Nachholen am Planende vorgemerkt. In 1.3 galten sie beim Fortsetzen
    als erledigt und wurden nie nachgeholt.
  - Reste im precheck werden dem letzten Zyklus des Werkzeugs zugeordnet, der
    ein Deployment gestartet hat, nicht dem letzten mit durchgefuehrtem Reset.
    Der Zuordnungsstand liegt in run/letzter_<phase>.json und uebersteht einen
    Neustart. In 1.3 landete ein Rest nach Halt vor dem Reset beim
    vorangehenden Zyklus.
  - Beim Beenden eines Werkzeugs werden nach dem Ende des Hauptprozesses
    verbliebene Mitglieder seiner Prozessgruppe (z. B. Terraform-Provider)
    hart beendet, damit keine verwaisten Prozesse zurueckbleiben.

Ein Zyklus: precheck -> deploy -> validate -> reset, immer vollstaendig.
Gemessen wird mit monotoner Uhr um den Werkzeugaufruf (Hauptkennzahl),
nicht aus Werkzeugausgaben. validate_vms.py entscheidet ueber Erfolg oder
Fehler, nicht ueber die Zeit. Der Wrapper protokolliert alle Rohdaten und
wendet zusaetzlich die vorab festgelegte Fehlerdefinition an
(abgeleitete Spalten, s. klassifiziere()).

Ausgaben:
  results/zyklen_<phase>.csv  eine Zeile je Zyklus
  results/vms_<phase>.csv     eine Zeile je VM
  logs/<phase>/<id>_.../      Ausgabe jedes Aufrufs, vCenter-Tasks und -Events
  logs/wrapper.log            Ablaufprotokoll
  run/status.json             aktueller Stand, bei jedem Phasenwechsel ueberschrieben
"""
import argparse
import csv
import datetime as dt
import fcntl
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

import yaml

import validate_vms as vv

WRAPPER_VERSION = "1.4"

# ------------------------------------------------------------------ Pfade
SCRIPT_DIR = Path(__file__).resolve().parent   # ~/iac_lab/scripts
BASE = SCRIPT_DIR.parent                       # ~/iac_lab
TF_DIR = BASE / "terraform"
AN_DIR = BASE / "ansible"
INVENTORY = BASE / "inventory.yaml"
VALIDATE = SCRIPT_DIR / "validate_vms.py"
RUN_DIR = BASE / "run"
LOG_DIR = BASE / "logs"
RES_DIR = BASE / "results"
ANSIBLE_PLAYBOOK = BASE / "venv-wab" / "bin" / "ansible-playbook"
TERRAFORM = "terraform"

# ------------------------------------------------- Feste Messparameter
# Bewusst fest codiert und nicht per Aufruf aenderbar; jede Zeile der
# Ergebnisdateien protokolliert sie. Waiter-Timeouts stehen in inventory.yaml
# (timeouts_min), weil beide Werkzeuge sie dort lesen.
VALIDATE_TIMEOUT_S = 600
VALIDATE_INTERVAL_S = 5
PAUSE_S = 60                        # nach jedem Zyklus, auch nach ungueltigen
RETRY_WAITS_S = (300, 900, 1800)    # Exit-Code 2: Wartezeiten vor Wiederholung
WATCHDOG_DEPLOY_S = 6 * 3600        # Notbremse gegen haengende Werkzeuge; muss ueber
                                    # clone + customization + guest_net liegen (lade_inventar)
WATCHDOG_RESET_S = 3600
WATCHDOG_AUFRAEUMEN_S = 3600
WATCHDOG_VALIDATE_S = VALIDATE_TIMEOUT_S + 600
MAX_NACHHOL = 3                     # je Planeintrag, danach Halt
VC_FENSTER_VORLAUF_S = 120          # Zeitfenster der Task-/Event-Abfrage
STUFEN_ERLAUBT = (1, 2, 4, 8, 16, 32)
WERKZEUGE = ("tf", "an")
PHASEN = ("trocken", "vorlauf", "vorlauf32", "haupt")
FALLBACK_RE = re.compile(r"WAB-FALLBACK \S+ \S+:")

TF_ENV = {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
AN_ENV = {"ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": "0"}
PFLICHT_ENV = ("VMWARE_HOST", "VMWARE_USER", "VMWARE_PASSWORD",
               "VSPHERE_SERVER", "VSPHERE_USER", "VSPHERE_PASSWORD")

ZYKLUS_FELDER = [
    "phase", "plan_id", "runde", "stufe", "werkzeug", "nachgeholt_fuer",
    "status", "ungueltig_grund",
    "start_utc", "deploy_s", "deploy_exit", "validate_s", "validate_exit",
    "validate_abfragen", "reset_s", "reset_exit",
    "vm_anzahl", "vm_fehler", "ursachen", "exit_abweichung",
    "fallback_marker", "harte_abschaltungen", "poweron_tasks",
    "reste_tf", "reste_an", "reste_fremd", "reset_fehler_fuer",
    "vc_daten", "vc_uhr_abweichung_s", "clone_tasks",
    "t_clone_erste_einreihung_s", "t_clone_letzter_start_s",
    "t_clone_letzter_abschluss_s", "t_letzte_customization_s", "meldelatenz_s",
    "timeouts_min", "validate_timeout_s", "validate_intervall_s", "pause_s",
    "seed", "wrapper_version", "logverzeichnis",
]
VM_FELDER = [
    "phase", "plan_id", "werkzeug", "stufe", "vm", "slot", "ok", "ursache",
    *vv.CRITERIA, "details",
    "t_customization_start_s", "t_customization_ok_s", "customization_fehler",
    "poweron_tasks", "harte_abschaltungen",
]


class Halt(Exception):
    """Messreihe anhalten; Grund steht in der Statusdatei."""


class Abbruch(BaseException):
    """SIGINT, SIGTERM oder SIGHUP: laufendes Werkzeug beenden, dann aussteigen."""


def _signal(signum, _frame):
    raise Abbruch(signal.Signals(signum).name)


# ------------------------------------------------------------ Hilfen

def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.isoformat(timespec="milliseconds") if t else ""


def r3(x):
    return "" if x is None else f"{x:.3f}"


def log(msg):
    line = f"{iso(utcnow())} {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "wrapper.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


STATUS = {}


def status(**kw):
    """Statusdatei atomar ueberschreiben."""
    STATUS.update(kw)
    STATUS["aktualisiert_utc"] = iso(utcnow())
    STATUS["pid"] = os.getpid()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUN_DIR / "status.json.tmp"
    tmp.write_text(json.dumps(STATUS, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, RUN_DIR / "status.json")


def schreibe_zeile(path, felder, zeile):
    neu = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=felder, extrasaction="raise")
        if neu:
            w.writeheader()
        w.writerow({k: zeile.get(k, "") for k in felder})
        f.flush()
        os.fsync(f.fileno())


def lies_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def schreibe_json(path, doc):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------ Inventar

def lade_inventar():
    slots, prefixes = vv.load_inventory(str(INVENTORY))
    with open(INVENTORY, encoding="utf-8") as f:
        data = yaml.load(f, Loader=yaml.BaseLoader)
    template = str(data.get("template_name") or "").strip()
    if not template:
        raise vv.ConfigError("template_name fehlt in inventory.yaml")
    tm = data.get("timeouts_min") or {}
    timeouts = ",".join(f"{k}={tm[k]}" for k in sorted(tm))
    max_stufe = max(s for s in STUFEN_ERLAUBT if s <= len(slots))
    waiter = {}
    for k in ("clone", "customization", "guest_net"):
        try:
            waiter[k] = int(tm.get(k))
        except (TypeError, ValueError):
            raise vv.ConfigError(f"timeouts_min.{k} fehlt oder ist keine ganze Zahl")
    # Ein haengender Terraform-Zyklus muss an einem Waiter-Timeout scheitern
    # (fehlerhaft), nicht am Watchdog (ungueltig, wuerde nachgeholt).
    if sum(waiter.values()) * 60 >= WATCHDOG_DEPLOY_S:
        raise vv.ConfigError(
            f"WATCHDOG_DEPLOY_S ({WATCHDOG_DEPLOY_S // 60} min) liegt nicht ueber "
            f"clone + customization + guest_net ({sum(waiter.values())} min)")
    clone_min = waiter["clone"]
    return {"slots": slots, "prefixes": prefixes, "template": template,
            "timeouts": timeouts, "max_stufe": max_stufe, "clone_min": clone_min}


def vm_namen(inv, werkzeug, stufe):
    return [inv["prefixes"][werkzeug] + sid for sid in sorted(inv["slots"])[:stufe]]


# ------------------------------------------------------------ Aufrufe

def beende_gruppe(p):
    """Prozessgruppe des Werkzeugs beenden: erst SIGINT (Terraform beendet sich
    dann geordnet und schreibt den State), dann SIGTERM, zuletzt SIGKILL."""
    for sig, frist in ((signal.SIGINT, 60), (signal.SIGTERM, 30), (signal.SIGKILL, None)):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            return
        try:
            p.wait(timeout=frist)
            break
        except subprocess.TimeoutExpired:
            continue
    # Der Hauptprozess ist beendet; Kinder, die SIGINT ignorieren (Terraform-
    # Provider), koennen in der Gruppe zurueckbleiben und werden sonst verwaist.
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def aufruf(argv, cwd, logdatei, watchdog, env_extra=None):
    """Startet einen Prozess, misst seine Dauer mit monotoner Uhr.

    Das Werkzeug laeuft in eigener Prozessgruppe (Watchdog). Wird der Wrapper
    selbst unterbrochen, beendet er deshalb die Gruppe ausdruecklich, damit
    kein verwaistes Werkzeug weiterlaeuft.
    """
    env = os.environ.copy()
    env.update(env_extra or {})
    with open(logdatei, "w", encoding="utf-8") as f:
        f.write(f"# {' '.join(map(str, argv))}\n# cwd={cwd}\n")
        f.flush()
        start_utc = utcnow()
        t0 = time.monotonic()
        p = subprocess.Popen([str(a) for a in argv], cwd=cwd, stdout=f,
                             stderr=subprocess.STDOUT, env=env, start_new_session=True)
        watchdog_ausgeloest = False
        try:
            rc = p.wait(timeout=watchdog)
        except subprocess.TimeoutExpired:
            watchdog_ausgeloest = True
            beende_gruppe(p)
            rc = p.returncode
        except BaseException:
            f.write("\n# Wrapper unterbrochen, Werkzeug wird beendet\n")
            f.flush()
            beende_gruppe(p)
            raise
        dauer = time.monotonic() - t0
        ende_utc = utcnow()
        f.write(f"\n# exit={rc} dauer_s={dauer:.3f} watchdog={watchdog_ausgeloest}\n")
    return {"rc": rc, "dauer_s": dauer, "start_utc": start_utc, "ende_utc": ende_utc,
            "watchdog": watchdog_ausgeloest, "log": logdatei}


def validate_aufruf(args, logdatei):
    """validate_vms.py mit --json; liefert (exit_code, dokument, aufruf)."""
    argv = [sys.executable, VALIDATE, "--inventory", INVENTORY, "--json", *args]
    r = aufruf(argv, BASE, logdatei, WATCHDOG_VALIDATE_S)
    doc = None
    for zeile in reversed(Path(logdatei).read_text(encoding="utf-8").splitlines()):
        if zeile.startswith("{"):
            try:
                doc = json.loads(zeile)
            except ValueError:
                pass
            break
    rc = r["rc"]
    if r["watchdog"] or doc is None or rc not in (0, 1, 2, 3):
        rc = 2  # kein verwertbares Ergebnis: wie Verbindungsfehler behandeln
    return rc, doc, r


def mit_wiederholung(fn, was):
    """Exit-Code 3: sofort anhalten. Exit-Code 2: warten und wiederholen."""
    for versuch, warte in enumerate((0, *RETRY_WAITS_S), 1):
        if warte:
            log(f"{was}: vCenter nicht erreichbar, warte {warte // 60} min (Versuch {versuch})")
            status(phase_im_zyklus="warten", grund=f"{was}: Exit-Code 2, Versuch {versuch}")
            time.sleep(warte)
        rc, doc, r = fn(versuch)
        if rc == 3:
            raise Halt(f"{was}: Anmeldung abgelehnt (Exit-Code 3)")
        if rc != 2:
            return rc, doc, r
    raise Halt(f"{was}: vCenter nach {len(RETRY_WAITS_S)} Wiederholungen nicht erreichbar")


def terraform_state():
    r = subprocess.run([TERRAFORM, "state", "list"], cwd=TF_DIR, capture_output=True,
                       text=True, env={**os.environ, **TF_ENV})
    if r.returncode != 0:
        raise Halt(f"terraform state list fehlgeschlagen: {r.stderr.strip()[:300]}")
    return [z for z in r.stdout.splitlines() if z.strip()]


def precheck(cdir, name, inv):
    """Zielordner leer und Terraform-State leer. Liefert (sauber, befund)."""
    rc, doc, _ = mit_wiederholung(
        lambda v: validate_aufruf(["precheck"], cdir / f"{name}_{v}.log"), name)
    objekte = [o["name"] for o in (doc or {}).get("objekte", [])]
    state = terraform_state()
    befund = {
        "reste_tf": [o for o in objekte if o.startswith(inv["prefixes"]["tf"])],
        "reste_an": [o for o in objekte if o.startswith(inv["prefixes"]["an"])],
        "state": state,
    }
    befund["reste_fremd"] = [o for o in objekte
                             if o not in befund["reste_tf"] and o not in befund["reste_an"]]
    sauber = rc == 0 and not objekte and not state
    (cdir / f"{name}.json").write_text(json.dumps(befund, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    return sauber, befund


def alle_zyklus_namen(inv):
    """Explizite Liste aller Namen, die ein Zyklus anlegen kann (beide Werkzeuge)."""
    return {n for w in WERKZEUGE for n in vm_namen(inv, w, inv["max_stufe"])}


def warte_auf_vcenter_tasks(inv):
    """Warten, bis keine Task mehr an Zyklus-VMs oder am Template laeuft.

    Ein abgebrochenes Werkzeug stoppt seine Clone-Tasks im vCenter nicht; solange
    sie laufen, laesst sich die entstehende VM weder loeschen noch zuordnen.
    Obergrenze ist der Clone-Timeout aus inventory.yaml.
    """
    namen = alle_zyklus_namen(inv) | {inv["template"]}
    frist = time.monotonic() + inv["clone_min"] * 60
    while True:
        si = vv.connect()
        try:
            laufend = []
            for t in si.RetrieveContent().taskManager.recentTask or []:
                try:
                    info = t.info
                except Exception:
                    continue  # Task inzwischen aus der Liste gefallen
                if str(info.state) in ("queued", "running") and info.entityName in namen:
                    laufend.append(f"{info.descriptionId}@{info.entityName}")
        finally:
            try:
                from pyVim.connect import Disconnect
                Disconnect(si)
            except Exception:
                pass
        if not laufend:
            return
        if time.monotonic() >= frist:
            raise Halt(f"Aufraeumen: vCenter-Tasks laufen nach {inv['clone_min']} min noch: {laufend}")
        log(f"Aufraeumen: warte auf laufende vCenter-Tasks {laufend}")
        status(phase_im_zyklus="aufraeumen_warten", grund=f"laufende Tasks: {len(laufend)}")
        time.sleep(15)


def entferne_reste_neutral(inv):
    """Werkzeugneutrales Loeschen verbliebener Zyklus-VMs ueber die vSphere-API.

    Nur VMs im Zielordner, deren Name in der expliziten Namensliste steht und die
    kein Template sind -- nie ueber Muster oder "alles im Ordner". Faengt Reste
    ab, die keinem Werkzeug mehr gehoeren (z. B. ein nach dem Abbruch fertig
    gewordener Terraform-Klon, der nicht im State steht).
    """
    from pyVim.connect import Disconnect
    from pyVim.task import WaitForTask
    namen = alle_zyklus_namen(inv)
    si = vv.connect()
    try:
        folder = vv.find_folder(si.RetrieveContent(), "wab-benchmark")
        for vm in list(folder.childEntity):
            if not isinstance(vm, vv.vim.VirtualMachine) or vm.name not in namen:
                continue
            if vm.config is not None and vm.config.template:
                continue
            log(f"Aufraeumen (werkzeugneutral): {vm.name}")
            if str(vm.runtime.powerState) == "poweredOn":
                WaitForTask(vm.PowerOffVM_Task())
            WaitForTask(vm.Destroy_Task())
    finally:
        try:
            Disconnect(si)
        except Exception:
            pass


def aufraeumen(cdir, inv):
    """Aufraeumen ausserhalb der Messung.

    1. auf laufende vCenter-Tasks warten (abgebrochenes Werkzeug),
    2. beide Werkzeuge mit hoechster Stufe aufraeumen lassen,
    3. verbliebene Zyklus-VMs werkzeugneutral loeschen,
    4. Terraform-State-Eintraege ohne VM per Refresh entfernen.
    """
    m = inv["max_stufe"]
    log(f"Aufraeumen ausserhalb der Messung mit vm_count={m}")
    status(phase_im_zyklus="aufraeumen")
    warte_auf_vcenter_tasks(inv)
    r1 = aufruf([TERRAFORM, "destroy", "-auto-approve", "-input=false", "-no-color",
                 "-refresh=false", f"-parallelism={m}", "-var", f"vm_count={m}"],
                TF_DIR, cdir / "aufraeumen_tf.log", WATCHDOG_AUFRAEUMEN_S, TF_ENV)
    r2 = aufruf([ANSIBLE_PLAYBOOK, "-f", m, "-e", f"vm_count={m}", "cleanup.yml"],
                AN_DIR, cdir / "aufraeumen_an.log", WATCHDOG_AUFRAEUMEN_S, AN_ENV)
    if r1["watchdog"] or r2["watchdog"]:
        raise Halt("Aufraeumen: Watchdog ausgeloest")
    try:
        entferne_reste_neutral(inv)
    except Halt:
        raise
    except Exception as e:
        log(f"Aufraeumen (werkzeugneutral) fehlgeschlagen: {type(e).__name__}: {e}")
    if terraform_state():
        log("Aufraeumen: Terraform-State nicht leer, Abgleich per refresh-only")
        r3 = aufruf([TERRAFORM, "apply", "-refresh-only", "-auto-approve", "-input=false",
                     "-no-color", f"-parallelism={m}", "-var", f"vm_count={m}"],
                    TF_DIR, cdir / "aufraeumen_tf_refresh.log", WATCHDOG_AUFRAEUMEN_S, TF_ENV)
        if r3["watchdog"]:
            raise Halt("Aufraeumen: Watchdog beim Terraform-Refresh ausgeloest")


def werkzeug_argv(werkzeug, aktion, p):
    if werkzeug == "tf":
        verb = "apply" if aktion == "deploy" else "destroy"
        return ([TERRAFORM, verb, "-auto-approve", "-input=false", "-no-color",
                 f"-parallelism={p}", "-var", f"vm_count={p}"], TF_DIR, TF_ENV)
    playbook = "deploy.yml" if aktion == "deploy" else "cleanup.yml"
    return [ANSIBLE_PLAYBOOK, "-f", p, "-e", f"vm_count={p}", playbook], AN_DIR, AN_ENV


# ------------------------------------------------------ vCenter-Daten

def _vc_zeit(t, abweichung):
    """vCenter-Zeitstempel auf die Uhr des Control-Node umrechnen."""
    return t - dt.timedelta(seconds=abweichung)


def _lies_collector(collector, lese):
    eintraege = []
    try:
        collector.RewindCollector()
        while True:
            seite = lese(100)
            if not seite:
                break
            eintraege.extend(seite)
    finally:
        collector.DestroyCollector()
    return eintraege


def hole_vcenter(cdir, deploy, reset):
    """Tasks und Events des Zyklus abholen und roh ablegen.

    Liefert (abweichung_s, tasks, events) oder wirft eine Ausnahme.
    Zeitstempel bleiben in vCenter-Zeit; abweichung_s = vCenter - Control-Node.
    """
    from pyVim.connect import Disconnect
    from pyVmomi import vim

    si = vv.connect()
    try:
        content = si.RetrieveContent()
        l0 = utcnow()
        vc = si.CurrentTime()
        l1 = utcnow()
        abweichung = (vc - (l0 + (l1 - l0) / 2)).total_seconds()
        beginn = deploy["start_utc"] + dt.timedelta(seconds=abweichung - VC_FENSTER_VORLAUF_S)
        ende = vc + dt.timedelta(seconds=5)

        tspec = vim.TaskFilterSpec(time=vim.TaskFilterSpec.ByTime(
            timeType="queuedTime", beginTime=beginn, endTime=ende))
        tc = content.taskManager.CreateCollectorForTasks(tspec)
        tasks = [{
            "key": t.key, "beschreibung": t.descriptionId, "objekt": t.entityName,
            "zustand": str(t.state), "einreihung": iso(t.queueTime),
            "start": iso(t.startTime), "ende": iso(t.completeTime),
            "fehler": (getattr(t.error, "msg", None) or type(t.error).__name__) if t.error else None,
        } for t in _lies_collector(tc, tc.ReadNextTasks)]

        espec = vim.event.EventFilterSpec(time=vim.event.EventFilterSpec.ByTime(
            beginTime=beginn, endTime=ende))
        ec = content.eventManager.CreateCollectorForEvents(espec)
        events = [{
            "typ": type(e).__name__, "key": e.key, "zeit": iso(e.createdTime),
            "vm": e.vm.name if getattr(e, "vm", None) else None,
            "text": e.fullFormattedMessage,
        } for e in _lies_collector(ec, ec.ReadNextEvents)]
    finally:
        try:
            Disconnect(si)
        except Exception:
            pass
    schreibe_json(cdir / "vcenter.json", {
        "abweichung_s": abweichung, "fenster_beginn_vc": iso(beginn), "fenster_ende_vc": iso(ende),
        "deploy_start_utc": iso(deploy["start_utc"]), "deploy_ende_utc": iso(deploy["ende_utc"]),
        "reset_start_utc": iso(reset["start_utc"]), "reset_ende_utc": iso(reset["ende_utc"]),
        "tasks": tasks, "events": events})
    return abweichung, tasks, events


def werte_vcenter(abweichung, tasks, events, namen, template, deploy, reset):
    """Kennzahlen je Welle und je VM aus den Rohdaten (Zeiten relativ zum Deploy-Start)."""
    def lokal(s):
        return _vc_zeit(dt.datetime.fromisoformat(s), abweichung) if s else None

    def rel(t):
        return (t - deploy["start_utc"]).total_seconds() if t else None

    def im(t, fenster):
        return t is not None and fenster["start_utc"] <= t <= fenster["ende_utc"]

    clones = [t for t in tasks if t["beschreibung"] == "VirtualMachine.clone"
              and t["objekt"] == template and im(lokal(t["einreihung"]), deploy)]
    welle = {"clone_tasks": len(clones)}
    if clones:
        welle["t_clone_erste_einreihung_s"] = rel(min(lokal(t["einreihung"]) for t in clones))
        starts = [lokal(t["start"]) for t in clones if t["start"]]
        enden = [lokal(t["ende"]) for t in clones if t["ende"]]
        if len(starts) == len(clones):
            welle["t_clone_letzter_start_s"] = rel(max(starts))
        if len(enden) == len(clones):
            welle["t_clone_letzter_abschluss_s"] = rel(max(enden))

    je_vm = {}
    for n in namen:
        ev = [e for e in events if e["vm"] == n and im(lokal(e["zeit"]), deploy)]
        start = [lokal(e["zeit"]) for e in ev if e["typ"].endswith("CustomizationStartedEvent")]
        ok = [lokal(e["zeit"]) for e in ev if e["typ"].endswith("CustomizationSucceeded")]
        fehl = [e for e in ev if e["typ"].endswith("CustomizationFailed")
                or "CustomizationSysprepFailed" in e["typ"]
                or "CustomizationUnknownFailure" in e["typ"]]
        an = [t for t in tasks if t["beschreibung"] == "VirtualMachine.powerOn"
              and t["objekt"] == n and im(lokal(t["einreihung"]), deploy)]
        aus = [t for t in tasks if t["beschreibung"] == "VirtualMachine.powerOff"
               and t["objekt"] == n and im(lokal(t["einreihung"]), reset)]
        je_vm[n] = {
            "t_customization_start_s": rel(max(start)) if start else None,
            "t_customization_ok_s": rel(max(ok)) if ok else None,
            "customization_fehler": len(fehl),
            "poweron_tasks": len(an),
            "harte_abschaltungen": len(aus),
        }
    oks = [v["t_customization_ok_s"] for v in je_vm.values()]
    if oks and all(x is not None for x in oks):
        welle["t_letzte_customization_s"] = max(oks)
        welle["meldelatenz_s"] = deploy["dauer_s"] - max(oks)
    welle["poweron_tasks"] = sum(v["poweron_tasks"] for v in je_vm.values())
    welle["harte_abschaltungen"] = sum(v["harte_abschaltungen"] for v in je_vm.values())
    return welle, je_vm


# ------------------------------------------------- Fehlerdefinition

def klassifiziere(vdoc, deploy_exit):
    """Vorab festgelegte Fehlerdefinition.

    - Fehlerhaft ist ein Zyklus, sobald validate fuer mindestens eine VM
      scheitert. Werkzeug-Exit-Codes sind kein Fehlerkriterium; eine
      Abweichung zwischen Exit-Code und Validierung wird nur markiert.
    - Ursache je VM: erstes gescheitertes Kriterium in Kausalreihenfolge
      exists -> power -> tools -> ip -> hostname; die uebrigen sind Folgefehler.
    - Zyklus- und VM-Fehlerrate ergeben sich aus status bzw. vm_fehler/vm_anzahl.
    - Reste im naechsten precheck sind ein Reset-Fehler des verursachenden
      Zyklus (Spalte reset_fehler_fuer im spaeteren Zyklus).
    - In die Zeitstatistik gehen nur erfolgreiche Zyklen.
    - WAB-FALLBACK ist kein Fehler, nur gezaehlt.
    """
    vms = vdoc["vms"]
    ursachen = {}
    for v in vms:
        erste = next((k["name"] for k in v["kriterien"] if not k["ok"]), "")
        v["ursache"] = erste
        if erste:
            ursachen[erste] = ursachen.get(erste, 0) + 1
    fehler = sum(1 for v in vms if not v["ok"])
    status_ = "erfolgreich" if fehler == 0 else "fehlerhaft"
    return {
        "status": status_,
        "vm_anzahl": len(vms),
        "vm_fehler": fehler,
        "ursachen": ";".join(f"{k}:{n}" for k, n in sorted(ursachen.items())),
        "exit_abweichung": int((deploy_exit != 0) == (status_ == "erfolgreich")),
    }


# ------------------------------------------------------------ Plan

def plan_pfad(phase):
    return RUN_DIR / f"plan_{phase}.json"


def cmd_plan(a):
    pfad = plan_pfad(a.phase)
    if pfad.exists():
        sys.exit(f"{pfad} existiert bereits; bewusst nicht ueberschrieben (vorher selbst loeschen)")
    stufen = list(dict.fromkeys(a.stufen))
    falsch = [s for s in stufen if s not in STUFEN_ERLAUBT]
    if falsch:
        sys.exit(f"Stufen nicht erlaubt: {falsch} (erlaubt: {STUFEN_ERLAUBT})")
    try:
        inv = lade_inventar()
    except vv.ConfigError as e:
        sys.exit(f"Konfiguration: {e}")
    if max(stufen) > len(inv["slots"]):
        sys.exit(f"Stufe {max(stufen)} > Slots im Inventar ({len(inv['slots'])})")
    werkzeuge = list(dict.fromkeys(a.werkzeuge))
    rng = random.Random(a.seed)
    eintraege, nr = [], 1
    for runde in range(1, a.runden + 1):
        reihenfolge = stufen[:]
        rng.shuffle(reihenfolge)
        wz = werkzeuge if runde % 2 == 1 else list(reversed(werkzeuge))
        for s in reihenfolge:
            for w in wz:
                eintraege.append({"id": nr, "runde": runde, "stufe": s, "werkzeug": w})
                nr += 1
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    schreibe_json(pfad, {
        "phase": a.phase, "seed": a.seed, "runden": a.runden, "stufen": stufen,
        "werkzeuge": werkzeuge, "erzeugt_utc": iso(utcnow()),
        "wrapper_version": WRAPPER_VERSION, "eintraege": eintraege})
    print(f"{pfad}: {len(eintraege)} Zyklen, {a.runden} Runden, Stufen {stufen}, "
          f"Werkzeuge {werkzeuge}, Seed {a.seed}")
    return 0


def letzter_pfad(phase):
    return RUN_DIR / f"letzter_{phase}.json"


def merke_deploy(ctx, werkzeug, pid):
    """Vor dem Deploy festhalten, welcher Zyklus zuletzt VMs dieses Werkzeugs
    angelegt haben kann; Grundlage fuer reset_fehler_fuer im naechsten precheck."""
    ctx["letzter"][werkzeug] = pid
    schreibe_json(letzter_pfad(ctx["phase"]), ctx["letzter"])


def nachholen(plan, eintrag, phase):
    wurzel = eintrag.get("nachgeholt_fuer") or eintrag["id"]
    bisher = sum(1 for e in plan["eintraege"] if e.get("nachgeholt_fuer") == wurzel)
    if bisher >= MAX_NACHHOL:
        raise Halt(f"Planeintrag {wurzel}: {bisher} Nachholversuche ungueltig, systematisches Problem")
    neu = {"id": max(e["id"] for e in plan["eintraege"]) + 1, "runde": eintrag["runde"],
           "stufe": eintrag["stufe"], "werkzeug": eintrag["werkzeug"], "nachgeholt_fuer": wurzel}
    plan["eintraege"].append(neu)
    schreibe_json(plan_pfad(phase), plan)
    log(f"Planeintrag {eintrag['id']} ungueltig, wird als {neu['id']} am Planende nachgeholt")


# ------------------------------------------------------------ Zyklus

def zyklus(eintrag, plan, ctx):
    phase, inv = ctx["phase"], ctx["inv"]
    pid, w, p = eintrag["id"], eintrag["werkzeug"], eintrag["stufe"]
    cdir = LOG_DIR / phase / f"{pid:04d}_{w}_p{p:02d}"
    if cdir.exists():  # unterbrochener frueherer Versuch: nicht ueberschreiben
        cdir = cdir.with_name(cdir.name + "_" + utcnow().strftime("%Y%m%dT%H%M%S"))
    cdir.mkdir(parents=True)
    namen = vm_namen(inv, w, p)
    zeile = {
        "phase": phase, "plan_id": pid, "runde": eintrag["runde"], "stufe": p, "werkzeug": w,
        "nachgeholt_fuer": eintrag.get("nachgeholt_fuer", ""),
        "timeouts_min": inv["timeouts"], "validate_timeout_s": VALIDATE_TIMEOUT_S,
        "validate_intervall_s": VALIDATE_INTERVAL_S, "pause_s": PAUSE_S,
        "seed": plan["seed"], "wrapper_version": WRAPPER_VERSION,
        "logverzeichnis": str(cdir.relative_to(BASE)),
    }
    zdatei = RES_DIR / f"zyklen_{phase}.csv"
    vdatei = RES_DIR / f"vms_{phase}.csv"
    status(zyklus=pid, runde=eintrag["runde"], stufe=p, werkzeug=w, grund="")
    log(f"Zyklus {pid}: Runde {eintrag['runde']}, Stufe {p}, Werkzeug {w}")

    # ---- precheck
    status(phase_im_zyklus="precheck")
    sauber, befund = precheck(cdir, "precheck", inv)
    if not sauber:
        zuordnung = []
        for wz, reste in (("tf", befund["reste_tf"] or befund["state"]), ("an", befund["reste_an"])):
            if reste:
                zuordnung.append(f"{wz}:{ctx['letzter'].get(wz, 'unbekannt')}")
        zeile.update(status="ungueltig", ungueltig_grund="precheck",
                     reste_tf=len(befund["reste_tf"]) + len(befund["state"]),
                     reste_an=len(befund["reste_an"]), reste_fremd=len(befund["reste_fremd"]),
                     reset_fehler_fuer=";".join(zuordnung))
        log(f"Zyklus {pid}: precheck nicht sauber {befund}")
        aufraeumen(cdir, inv)
        sauber2, befund2 = precheck(cdir, "precheck_nach_aufraeumen", inv)
        schreibe_zeile(zdatei, ZYKLUS_FELDER, zeile)
        if not sauber2:
            raise Halt(f"Zyklus {pid}: Reste nach Aufraeumen: {befund2}")
        nachholen(plan, eintrag, phase)
        pause()
        return

    # ---- deploy (Hauptkennzahl)
    merke_deploy(ctx, w, pid)
    status(phase_im_zyklus="deploy")
    argv, cwd, env = werkzeug_argv(w, "deploy", p)
    d = aufruf(argv, cwd, cdir / "deploy.log", WATCHDOG_DEPLOY_S, env)
    zeile.update(start_utc=iso(d["start_utc"]), deploy_s=r3(d["dauer_s"]), deploy_exit=d["rc"])
    if d["watchdog"]:
        zeile.update(status="ungueltig", ungueltig_grund="watchdog_deploy")
        schreibe_zeile(zdatei, ZYKLUS_FELDER, zeile)
        nachholen(plan, eintrag, phase)
        raise Halt(f"Zyklus {pid}: Watchdog im Deploy ausgeloest, Eingriff noetig")

    # ---- validate (Erfolg oder Fehler)
    status(phase_im_zyklus="validate")
    rc, vdoc, v = validate_aufruf(
        ["validate", "--tool", w, "--count", str(p),
         "--timeout", str(VALIDATE_TIMEOUT_S), "--interval", str(VALIDATE_INTERVAL_S)],
        cdir / "validate.log")
    zeile.update(validate_s=r3(v["dauer_s"]), validate_exit=rc,
                 validate_abfragen=(vdoc or {}).get("abfragen", ""))
    if rc == 3:
        zeile.update(status="ungueltig", ungueltig_grund="anmeldung")
        schreibe_zeile(zdatei, ZYKLUS_FELDER, zeile)
        nachholen(plan, eintrag, phase)
        raise Halt(f"Zyklus {pid}: Anmeldung abgelehnt (Exit-Code 3)")
    verbindung = rc == 2
    if verbindung:
        log(f"Zyklus {pid}: validate ohne Ergebnis (Exit-Code 2), Zyklus ungueltig")
        mit_wiederholung(lambda n: validate_aufruf(["precheck"], cdir / f"erreichbar_{n}.log"),
                         "Erreichbarkeit")

    # ---- reset (immer)
    status(phase_im_zyklus="reset")
    argv, cwd, env = werkzeug_argv(w, "reset", p)
    r = aufruf(argv, cwd, cdir / "reset.log", WATCHDOG_RESET_S, env)
    zeile.update(reset_s=r3(r["dauer_s"]), reset_exit=r["rc"],
                 fallback_marker=len(FALLBACK_RE.findall(
                     Path(r["log"]).read_text(encoding="utf-8", errors="replace"))))
    if r["watchdog"]:
        zeile.update(status="ungueltig", ungueltig_grund="watchdog_reset")
        schreibe_zeile(zdatei, ZYKLUS_FELDER, zeile)
        nachholen(plan, eintrag, phase)
        raise Halt(f"Zyklus {pid}: Watchdog im Reset ausgeloest, Eingriff noetig")

    # ---- vCenter-Tasks und -Events (direkt nach dem Zyklus)
    status(phase_im_zyklus="vcenter_daten")
    welle, je_vm = {}, {}
    try:
        abw, tasks, events = hole_vcenter(cdir, d, r)
        welle, je_vm = werte_vcenter(abw, tasks, events, namen, inv["template"], d, r)
        zeile.update(vc_daten="ok", vc_uhr_abweichung_s=r3(abw))
    except Exception as e:  # Rohdaten fehlen, Zyklus bleibt gueltig
        zeile["vc_daten"] = f"fehler: {type(e).__name__}"
        log(f"Zyklus {pid}: vCenter-Daten nicht abholbar: {type(e).__name__}: {e}")
        (cdir / "vcenter_fehler.txt").write_text(traceback.format_exc(), encoding="utf-8")
    for k, x in welle.items():
        zeile[k] = r3(x) if isinstance(x, float) else x

    # ---- Ergebnis
    if verbindung:
        zeile.update(status="ungueltig", ungueltig_grund="verbindung")
        schreibe_zeile(zdatei, ZYKLUS_FELDER, zeile)
        nachholen(plan, eintrag, phase)
    else:
        zeile.update(klassifiziere(vdoc, d["rc"]))
        schreibe_zeile(zdatei, ZYKLUS_FELDER, zeile)
        for vm in vdoc["vms"]:
            krit = {k["name"]: int(k["ok"]) for k in vm["kriterien"]}
            ex = je_vm.get(vm["vm"], {})
            schreibe_zeile(vdatei, VM_FELDER, {
                "phase": phase, "plan_id": pid, "werkzeug": w, "stufe": p,
                "vm": vm["vm"], "slot": vm["slot"], "ok": int(vm["ok"]),
                "ursache": vm["ursache"], **{c: krit.get(c, "") for c in vv.CRITERIA},
                "details": " | ".join(f"{k['name']}={k['detail']}" for k in vm["kriterien"]),
                **{k: (r3(x) if isinstance(x, float) else ("" if x is None else x))
                   for k, x in ex.items()}})
        log(f"Zyklus {pid}: {zeile['status']}, deploy {zeile['deploy_s']} s, "
            f"reset {zeile['reset_s']} s, VM-Fehler {zeile['vm_fehler']}")
    status(letztes_ergebnis={k: zeile.get(k, "") for k in
                             ("plan_id", "status", "ungueltig_grund", "deploy_s", "reset_s", "vm_fehler")})
    pause()


def pause():
    status(phase_im_zyklus="pause")
    time.sleep(PAUSE_S)


# ------------------------------------------------------------ Lauf

def cmd_run(a):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(RUN_DIR / "wrapper.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Ein Wrapper laeuft bereits (run/wrapper.lock gesperrt).")
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _signal)
    STATUS.clear()
    status(zustand="laeuft", phase=a.phase, gestartet_utc=iso(utcnow()), grund="")
    try:
        fehlend = [v for v in PFLICHT_ENV if not os.environ.get(v)]
        if fehlend:
            raise Halt(f"Umgebungsvariablen fehlen: {fehlend} (secrets.env geladen?)")
        if not ANSIBLE_PLAYBOOK.exists():
            raise Halt(f"{ANSIBLE_PLAYBOOK} nicht gefunden")
        pfad = plan_pfad(a.phase)
        if not pfad.exists():
            raise Halt(f"{pfad} fehlt; zuerst 'plan' aufrufen")
        plan = json.loads(pfad.read_text(encoding="utf-8"))
        inv = lade_inventar()
        if max(e["stufe"] for e in plan["eintraege"]) > len(inv["slots"]):
            raise Halt("Plan enthaelt Stufen ueber der Slotzahl im Inventar")

        log(f"Start Phase {a.phase}, Wrapper {WRAPPER_VERSION}, Plan mit {len(plan['eintraege'])} Eintraegen")
        status(phase_im_zyklus="consistency")
        LOG_DIR.joinpath(a.phase).mkdir(parents=True, exist_ok=True)
        stempel = utcnow().strftime("%Y%m%dT%H%M%S")
        rc, _, _ = mit_wiederholung(
            lambda n: validate_aufruf(["consistency"],
                                      LOG_DIR / a.phase / f"consistency_{stempel}_{n}.log"),
            "consistency")
        if rc != 0:
            raise Halt("consistency: Specs und inventory.yaml stimmen nicht ueberein")

        erledigt, letzter = set(), {}
        for z in lies_csv(RES_DIR / f"zyklen_{a.phase}.csv"):
            erledigt.add(int(z["plan_id"]))
            if z["deploy_s"]:  # Fallback fuer Reihen ohne letzter_<phase>.json (bis 1.3)
                letzter[z["werkzeug"]] = int(z["plan_id"])
        lp = letzter_pfad(a.phase)
        if lp.exists():
            letzter = {k: int(v) for k, v in json.loads(lp.read_text(encoding="utf-8")).items()}
        ctx = {"phase": a.phase, "inv": inv, "letzter": letzter}

        i = 0
        while i < len(plan["eintraege"]):  # Liste waechst durch Nachholen
            eintrag = plan["eintraege"][i]
            i += 1
            if eintrag["id"] in erledigt:
                continue
            stop = RUN_DIR / "STOP"
            if stop.exists():
                stop.unlink()
                raise Halt("Stopp angefordert (run/STOP)")
            offen = sum(1 for e in plan["eintraege"] if e["id"] not in erledigt) - 1
            status(offen_nach_diesem=offen)
            zyklus(eintrag, plan, ctx)
            erledigt.add(eintrag["id"])
        log(f"Phase {a.phase} vollstaendig abgearbeitet")
        status(zustand="fertig", phase_im_zyklus="", grund="")
        return 0
    except Halt as e:
        log(f"ANGEHALTEN: {e}")
        status(zustand="angehalten", grund=str(e))
        return 1
    except vv.ConfigError as e:
        log(f"ANGEHALTEN (Konfiguration): {e}")
        status(zustand="angehalten", grund=f"Konfiguration: {e}")
        return 1
    except Abbruch as e:
        log(f"ABGEBROCHEN durch Signal {e}; laufendes Werkzeug wurde beendet")
        status(zustand="abgebrochen", grund=f"Signal {e}")
        return 130
    except Exception as e:
        log(f"ABGEBROCHEN (intern): {type(e).__name__}: {e}\n{traceback.format_exc()}")
        status(zustand="abgebrochen", grund=f"intern: {type(e).__name__}: {e}")
        return 2


def cmd_status(_a):
    s = RUN_DIR / "status.json"
    print(s.read_text(encoding="utf-8") if s.exists() else "keine Statusdatei")
    for phase in PHASEN:
        p = plan_pfad(phase)
        if not p.exists():
            continue
        plan = json.loads(p.read_text(encoding="utf-8"))
        zeilen = lies_csv(RES_DIR / f"zyklen_{phase}.csv")
        zaehl = {}
        for z in zeilen:
            zaehl[z["status"]] = zaehl.get(z["status"], 0) + 1
        print(f"{phase}: {len(zeilen)}/{len(plan['eintraege'])} Planeintraege protokolliert {zaehl}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan", help="Ablaufplan erzeugen")
    pl.add_argument("--phase", choices=PHASEN, required=True)
    pl.add_argument("--runden", type=int, required=True)
    pl.add_argument("--seed", type=int, required=True)
    pl.add_argument("--stufen", type=int, nargs="+", default=[1, 2, 4, 8, 16, 32])
    pl.add_argument("--werkzeuge", nargs="+", choices=WERKZEUGE, default=list(WERKZEUGE))
    ru = sub.add_parser("run", help="Plan abarbeiten (oder fortsetzen)")
    ru.add_argument("--phase", choices=PHASEN, required=True)
    sub.add_parser("status", help="Stand anzeigen")
    a = ap.parse_args()
    if a.cmd == "plan" and a.runden < 1:
        ap.error("--runden muss >= 1 sein")
    return {"plan": cmd_plan, "run": cmd_run, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())