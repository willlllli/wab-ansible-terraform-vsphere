#!/usr/bin/env python3
"""Konfigurationsvergleich der WAB "Terraform vs. Ansible".

Deckt Asymmetrien auf, die aus Voreinstellungen von Provider oder Modul
stammen: Unterscheidet sich ein Terraform-Klon vom Ansible-Klon (oder ein
Klon vom Template) in Einstellungen, die keiner bewusst gesetzt hat?
Liest ausschliesslich die vSphere-API, aendert nichts.

Modi:
  dump NAME [--out DATEI]
      Sichert vm.config (vollstaendig, ohne Auswahl) und die Speicherbelegung
      der VM bzw. des Templates NAME als JSON. Standardziel:
      logs/vmconfig/<NAME>_<UTC-Zeitstempel>.json (per .gitignore ausgenommen).
      Gibt eine Kurzuebersicht aus (u. a. Firmware, Hardware-Version, belegter
      Speicher), damit offene Protokollwerte des Templates gleich mit anfallen.

  diff DATEI_A DATEI_B [DATEI_C] [--alle]
      Vergleicht gesicherte Konfigurationen. Identitaets- und Laufzeitfelder
      (Name, UUIDs, MAC, Dateipfade, Zeitstempel, guestinfo.* u. a., s. IGNORIERT)
      werden ausgeblendet und nur gezaehlt; --alle zeigt auch sie.
      Geraete werden ueber ihre Beschriftung zugeordnet ("Hard disk 1",
      "Network adapter 1"), extraConfig ueber den Schluessel.

Exit-Codes: 0 = keine Abweichung (dump: gesichert), 1 = Abweichungen,
            2 = Konfigurations-, Verbindungs- oder interner Fehler,
            3 = Anmeldung abgelehnt.

Verbindung wie validate_vms.py ueber VMWARE_* aus secrets.env.
Die Dateien enthalten keine Gast-IP-Adressen (vm.config, nicht vm.guest).
"""
import argparse
import datetime as dt
import fnmatch
import json
import sys
from pathlib import Path

import validate_vms as vv

BASE = Path(__file__).resolve().parent.parent
DUMP_DIR = BASE / "logs" / "vmconfig"

# Pfadmuster (fnmatch) fuer Felder, die je VM zwangslaeufig verschieden sind
# oder nur Laufzeitzustand abbilden. Werden im diff ausgeblendet und gezaehlt.
IGNORIERT = [
    "config.name", "config.uuid", "config.instanceUuid", "config.locationId",
    "config.changeVersion", "config.modified", "config.createDate",
    "config.template", "config.files.*", "config.vmxConfigChecksum",
    "config.datastoreUrl*", "config.vmStorageObjectId", "config.keyId*",
    "config.hardware.device.*.macAddress",
    "config.hardware.device.*.backing.fileName",
    "config.hardware.device.*.backing.uuid",
    "config.hardware.device.*.backing.contentId",
    "config.hardware.device.*.backing.changeId",
    "config.hardware.device.*.backing.backingObjectId",
    "config.extraConfig.uuid.*", "config.extraConfig.vc.uuid",
    "config.extraConfig.nvram", "config.extraConfig.sched.swap.derivedName",
    "config.extraConfig.migrate.*", "config.extraConfig.ethernet*.generatedAddress*",
    "config.extraConfig.guestinfo.*", "config.extraConfig.toolsInstallManager.*",
    "config.extraConfig.vmware.tools.*", "config.extraConfig.monitor.phys_bits_used",
    "config.extraConfig.softPowerOff", "config.extraConfig.vmotion.*",
    "config.extraConfig.checkpoint.*", "config.extraConfig.viv.moid",
    "config.extraConfig.vmxstats.filename",
    "config.extraConfig.scsi*.sasWWID", "config.extraConfig.vm.genid*",
    "config.hardware.device.*.diskObjectId", "config.hardware.device.VMCI device.id",
    "*.dynamicProperty", "*.dynamicType",   # pyVmomi-intern, stets leer
    "speicher.*", "dateien*",
]


# ------------------------------------------------------------ Umwandlung

def _eigenschaften(obj):
    """Eigenschaftsnamen eines pyVmomi-DataObject."""
    liste = getattr(type(obj), "_GetPropertyList", None)
    if callable(liste):
        try:
            return [p.name for p in liste()]
        except TypeError:
            return [p.name for p in liste(obj)]
    if hasattr(obj, "_propList"):
        return [p.name for p in obj._propList]
    return [k for k in vars(obj) if not k.startswith("_")]


def _basisklassen():
    """Basisklassen der pyVmomi-Objekte; sie liegen in pyVmomi.VmomiSupport."""
    from pyVmomi.VmomiSupport import DataObject, ManagedObject
    return ManagedObject, DataObject


def zu_json(obj, tiefe=0):
    """pyVmomi-Objekte rekursiv in JSON-taugliche Werte umwandeln."""
    ManagedObject, DataObject = _basisklassen()
    if tiefe > 40:
        return "<zu tief>"
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return f"<{len(obj)} Byte>"
    if isinstance(obj, (list, tuple)):
        return [zu_json(x, tiefe + 1) for x in obj]
    if isinstance(obj, ManagedObject):
        return f"{getattr(obj, '_wsdlName', type(obj).__name__)}:{getattr(obj, '_moId', '?')}"
    if isinstance(obj, DataObject):
        d = {"_typ": type(obj).__name__}
        for name in _eigenschaften(obj):
            d[name] = zu_json(getattr(obj, name, None), tiefe + 1)
        return d
    return str(obj)


# ------------------------------------------------------------ dump

def iso_oder_none(t):
    return t.isoformat() if t else None


def finde_vm(content, name):
    from pyVmomi import vim
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        treffer = [v for v in view.view if v.name == name]
    finally:
        view.Destroy()
    if len(treffer) != 1:
        raise vv.ConfigError(f"VM {name!r}: {len(treffer)} Treffer, erwartet genau 1")
    return treffer[0]


def cmd_dump(name, out, aktualisieren=False):
    si = vv.connect()
    try:
        content = si.RetrieveContent()
        vm = finde_vm(content, name)
        if aktualisieren:
            # Loest nur die Neuberechnung der Speicherangaben aus, aendert die VM nicht
            vm.RefreshStorageInfo()
        config = vm.config
        storage = vm.summary.storage
        layout = vm.layoutEx
        dateien = [{"name": f.name, "typ": f.type, "byte": f.size,
                    "eigen_byte": getattr(f, "uniqueSize", None)}
                   for f in (getattr(layout, "file", None) or [])]
        doc = {
            "gesichert_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "vm": name,
            "config": zu_json(config),
            "speicher": {
                "committed_byte": getattr(storage, "committed", None),
                "uncommitted_byte": getattr(storage, "uncommitted", None),
                "unshared_byte": getattr(storage, "unshared", None),
                "stand_utc": iso_oder_none(getattr(storage, "timestamp", None)),
            },
            "dateien": dateien,
        }
    finally:
        try:
            from pyVim.connect import Disconnect
            Disconnect(si)
        except Exception:
            pass
    if out is None:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        stempel = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = DUMP_DIR / f"{name}_{stempel}.json"
    out = Path(out)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    c = doc["config"]
    hw = c.get("hardware") or {}
    boot = c.get("bootOptions") or {}
    committed = doc["speicher"]["committed_byte"]
    belegt = f"{committed / 1024**3:.2f}" if committed is not None else "?"
    summe = sum(f["byte"] or 0 for f in doc["dateien"])
    print(f"gesichert: {out}")
    print(f"vm={name};template={c.get('template')};firmware={c.get('firmware')};"
          f"version={c.get('version')};guestId={c.get('guestId')};"
          f"cpu={hw.get('numCPU')};ram_mb={hw.get('memoryMB')};"
          f"secure_boot={boot.get('efiSecureBootEnabled')};belegt_gib={belegt};"
          f"stand_speicherangabe={doc['speicher']['stand_utc']}")
    for f in sorted(doc["dateien"], key=lambda x: -(x["byte"] or 0)):
        print(f"  datei={f['name'].rsplit('/', 1)[-1]};typ={f['typ']};mib={(f['byte'] or 0) / 1024**2:.1f}")
    print(f"  summe_dateien_gib={summe / 1024**3:.2f}")
    return 0


# ------------------------------------------------------------ diff

def flach(wert, pfad, ziel):
    """Verschachtelte Struktur in {pfad: wert} ueberfuehren.

    Geraeteliste nach deviceInfo.label, extraConfig nach key indizieren,
    damit Reihenfolge und Geraeteschluessel die Zuordnung nicht verschieben.
    """
    if isinstance(wert, dict):
        for k, v in wert.items():
            if k == "_typ":
                continue
            flach(v, f"{pfad}.{k}" if pfad else k, ziel)
        if "_typ" in wert:
            ziel[f"{pfad}._typ"] = wert["_typ"]
    elif isinstance(wert, list):
        if pfad.endswith("hardware.device"):
            for g in wert:
                label = ((g.get("deviceInfo") or {}).get("label")) or f"key{g.get('key')}"
                flach(g, f"{pfad}.{label}", ziel)
        elif pfad.endswith("extraConfig"):
            for o in wert:
                ziel[f"{pfad}.{o.get('key')}"] = o.get("value")
        elif all(not isinstance(x, (dict, list)) for x in wert):
            ziel[pfad] = wert
        else:
            for i, x in enumerate(wert):
                flach(x, f"{pfad}[{i}]", ziel)
    else:
        ziel[pfad] = wert


def ignoriert(pfad):
    # Geraetebeschriftungen enthalten Leerzeichen; Muster mit * decken sie ab.
    return any(fnmatch.fnmatchcase(pfad, m) for m in IGNORIERT)


def cmd_diff(dateien, alle):
    docs = []
    for d in dateien:
        try:
            docs.append(json.loads(Path(d).read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            raise vv.ConfigError(f"{d}: nicht lesbar ({e})")
    namen = [d.get("vm", Path(f).stem) for d, f in zip(docs, dateien)]
    flache = []
    for d in docs:
        z = {}
        flach({"config": d.get("config"), "speicher": d.get("speicher")}, "", z)
        flache.append(z)

    fehlt = "<fehlt>"
    abweichend, versteckt = [], 0
    # Geraete, die nicht in allen Dateien vorkommen, als eine Zeile melden
    praefix = "config.hardware.device."
    geraete = [{p[len(praefix):].split(".")[0] for p in f if p.startswith(praefix)} for f in flache]
    unvollstaendig = sorted(set().union(*geraete) - set.intersection(*geraete))
    for g in unvollstaendig:
        abweichend.append((f"{praefix}{g}", ["vorhanden" if g in ge else fehlt for ge in geraete]))
    for pfad in sorted(set().union(*flache)):
        if any(pfad.startswith(f"{praefix}{g}.") for g in unvollstaendig):
            continue
        werte = [f.get(pfad, fehlt) for f in flache]
        if all(w == werte[0] for w in werte):
            continue
        if ignoriert(pfad) and not alle:
            versteckt += 1
            continue
        abweichend.append((pfad, werte))

    print("Vergleich: " + " | ".join(namen))
    for pfad, werte in abweichend:
        markierung = " (ignoriert)" if ignoriert(pfad) else ""
        print(f"\n{pfad}{markierung}")
        for n, w in zip(namen, werte):
            print(f"  {n}: {json.dumps(w, ensure_ascii=False)}")
    relevante = sum(1 for p, _ in abweichend if not ignoriert(p))
    print(f"\nergebnis={'OK' if relevante == 0 else 'ABWEICHUNG'};"
          f"abweichungen={relevante};ausgeblendet={versteckt}")
    return 0 if relevante == 0 else 1


# ------------------------------------------------------------ Aufruf

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="modus", required=True)
    d = sub.add_parser("dump", help="Konfiguration einer VM/eines Templates sichern")
    d.add_argument("name")
    d.add_argument("--out")
    d.add_argument("--aktualisieren", action="store_true",
                   help="vCenter vorher die Speicherangaben neu berechnen lassen (RefreshStorageInfo)")
    f = sub.add_parser("diff", help="gesicherte Konfigurationen vergleichen")
    f.add_argument("dateien", nargs="+")
    f.add_argument("--alle", action="store_true", help="auch ausgeblendete Felder zeigen")
    a = ap.parse_args()
    if a.modus == "diff" and not 2 <= len(a.dateien) <= 3:
        ap.error("diff erwartet zwei oder drei Dateien")
    try:
        if a.modus == "dump":
            return cmd_dump(a.name, a.out, a.aktualisieren)
        return cmd_diff(a.dateien, a.alle)
    except vv.ConfigError as e:
        print(f"FEHLER (konfiguration): {e}", file=sys.stderr)
        return vv.EXIT_ERROR
    except vv.vim.fault.InvalidLogin as e:
        print(f"FEHLER (anmeldung): {getattr(e, 'msg', e)}", file=sys.stderr)
        return vv.EXIT_AUTH
    except (vv.vmodl.MethodFault, OSError) as e:
        print(f"FEHLER (verbindung): {type(e).__name__}: {getattr(e, 'msg', e)}", file=sys.stderr)
        return vv.EXIT_ERROR
    except Exception as e:
        print(f"FEHLER (intern): {type(e).__name__}: {e}", file=sys.stderr)
        return vv.EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
