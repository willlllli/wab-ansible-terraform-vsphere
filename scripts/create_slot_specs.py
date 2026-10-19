#!/usr/bin/env python3
"""Legt fehlende Slot-Specs der WAB "Terraform vs. Ansible" an.

Fuer jeden Slot aus inventory.yaml, dessen Customization Spec im vCenter noch
nicht existiert, wird die Spec des Quell-Slots (Standard 01) dupliziert und
ausschliesslich die feste IPv4-Adresse der einzigen NIC-Zuordnung gesetzt.
Vorhandene Specs werden nie veraendert.

Ohne --apply laeuft das Skript im Trockenmodus und zeigt nur, was angelegt
wuerde. Nach jedem Anlegen wird die neue Spec erneut gelesen und gegen die
Quelle verglichen: Abweichen darf nur die Adresse.

Exit-Codes: 0 = bestanden, 1 = Nachpruefung fehlgeschlagen,
            2 = Konfigurations-, Verbindungs-, Aufruf- oder interner Fehler,
            3 = Anmeldung abgelehnt (keine Wiederholung: Sperrrisiko).
Gleiche Bedeutung wie in validate_vms.py.

Inventar-Parser und Verbindung stammen aus validate_vms.py (gleiches
Verzeichnis), damit beide Skripte dieselbe Inventarlogik verwenden.
Danach immer: validate_vms.py consistency.
"""
import argparse
import json
import sys
from pathlib import Path

from pyVim.connect import Disconnect
from pyVmomi import vim, vmodl

# pyVmomi 9: der Encoder liegt nicht mehr in VmomiSupport (dort nur noch ein
# Platzhalter, der eine Exception wirft). Je nach Paketaufbau ist
# pyVmomi.VmomiJSONEncoder ein eigenes Modul oder direkt die Klasse.
try:
    from pyVmomi.VmomiJSONEncoder import VmomiJSONEncoder
except ImportError:
    from pyVmomi import VmomiJSONEncoder

from validate_vms import ConfigError, connect, load_inventory

EXIT_OK, EXIT_FAIL, EXIT_ERROR, EXIT_AUTH = 0, 1, 2, 3
IP_PATH = "nicSettingMap/0/adapter/ip/ipAddress"


def flatten(obj, prefix=""):
    """Verschachtelte JSON-Struktur -> {pfad: blattwert}."""
    out = {}
    if isinstance(obj, dict):
        items = obj.items()
    elif isinstance(obj, list):
        items = enumerate(obj)
    else:
        return {prefix: obj}
    for k, v in items:
        out.update(flatten(v, f"{prefix}/{k}" if prefix else str(k)))
    return out


def spec_fields(spec):
    return flatten(json.loads(json.dumps(spec, cls=VmomiJSONEncoder)))


def fixed_ip_of(item, name):
    """Prueft die Netzstruktur einer Spec und liefert ihre feste Adresse."""
    nics = item.spec.nicSettingMap or []
    if len(nics) != 1:
        raise ConfigError(f"{name}: {len(nics)} NIC-Zuordnungen, erwartet 1")
    ip = nics[0].adapter.ip
    if not isinstance(ip, vim.vm.customization.FixedIp):
        raise ConfigError(f"{name}: keine feste Adresse")
    return ip


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # wie validate_vms.py: inventory.yaml eine Ebene ueber scripts/, unabhaengig vom Arbeitsverzeichnis
    p.add_argument("--inventory", default=str(Path(__file__).resolve().parent.parent / "inventory.yaml"))
    p.add_argument("--source-slot", default="01", help="Slot, dessen Spec dupliziert wird")
    p.add_argument("--apply", action="store_true", help="tatsaechlich anlegen (sonst Trockenmodus)")
    return p.parse_args()


def create_one(mgr, source_name, source_fields, sid, name, ip):
    """Dupliziert, setzt die Adresse, prueft. Liefert True bei bestandener Nachpruefung."""
    mgr.DuplicateCustomizationSpec(name=source_name, newName=name)
    try:
        item = mgr.GetCustomizationSpec(name)
        fixed_ip_of(item, name).ipAddress = ip
        mgr.OverwriteCustomizationSpec(item)
    except Exception:
        # Das Duplikat existiert jetzt mit der Adresse der Quelle. Ein erneuter
        # Lauf wuerde es als "vorhanden" ueberspringen -- daher laut melden.
        print(f"slot={sid};spec={name};HALB ANGELEGT: traegt die Adresse von {source_name}, "
              f"vor erneutem Lauf im vCenter loeschen", file=sys.stderr)
        raise

    # Nachpruefung gegen den gespeicherten Stand, nicht gegen das lokale Objekt
    stored = mgr.GetCustomizationSpec(name)
    ip_ok = fixed_ip_of(stored, name).ipAddress == ip
    new_fields = spec_fields(stored.spec)
    diff = sorted(k for k in set(source_fields) | set(new_fields)
                  if k != IP_PATH and source_fields.get(k) != new_fields.get(k))
    ok = ip_ok and not diff
    detail = "nur Adresse abweichend" if ok else (
        ("Adresse nicht gesetzt; " if not ip_ok else "")
        + (f"weitere abweichende Felder: {', '.join(diff)}" if diff else ""))
    print(f"slot={sid};spec={name};angelegt;pruefung={'OK' if ok else 'FAIL'};detail={detail}")
    return ok


def main():
    args = parse_args()
    si = None
    try:
        slots, _ = load_inventory(args.inventory)  # Praefixe hier nicht benoetigt
        if args.source_slot not in slots:
            raise ConfigError(f"Quell-Slot {args.source_slot!r} nicht in inventory.yaml")
        source_name, source_ip = slots[args.source_slot]

        si = connect()
        mgr = si.RetrieveContent().customizationSpecManager

        # Quelle muss existieren und selbst zur Inventardatei passen
        if not mgr.DoesCustomizationSpecExist(source_name):
            raise ConfigError(f"Quell-Spec {source_name!r} nicht vorhanden")
        source = mgr.GetCustomizationSpec(source_name)
        if fixed_ip_of(source, source_name).ipAddress != source_ip:
            raise ConfigError(f"Quell-Spec {source_name!r}: Adresse weicht von inventory.yaml ab")
        source_fields = spec_fields(source.spec)

        plan = []
        for sid in sorted(slots):
            name, ip = slots[sid]
            exists = mgr.DoesCustomizationSpecExist(name)
            print(f"slot={sid};spec={name};aktion={'vorhanden' if exists else 'anlegen'}")
            if not exists:
                plan.append((sid, name, ip))

        if not args.apply:
            print(f"trockenmodus;anzulegen={len(plan)};ohne --apply wurde nichts veraendert")
            return EXIT_OK

        ok_all = True
        for sid, name, ip in plan:
            ok_all &= create_one(mgr, source_name, source_fields, sid, name, ip)

        print(f"ergebnis={'OK' if ok_all else 'FAIL'};angelegt={len(plan)}")
        return EXIT_OK if ok_all else EXIT_FAIL
    except ConfigError as e:
        print(f"FEHLER (konfiguration): {e}", file=sys.stderr)
        return EXIT_ERROR
    except vim.fault.InvalidLogin as e:  # vor MethodFault: ist eine Unterklasse
        print(f"FEHLER (anmeldung): {getattr(e, 'msg', '') or type(e).__name__}", file=sys.stderr)
        return EXIT_AUTH
    except (vmodl.MethodFault, OSError) as e:
        print(f"FEHLER (verbindung): {type(e).__name__}: {getattr(e, 'msg', e)}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as e:  # nie als Exit-Code 1 (= Nachpruefung fehlgeschlagen) enden
        print(f"FEHLER (intern): {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if si is not None:
            try:
                Disconnect(si)
            except Exception:
                pass  # tote Verbindung darf das Ergebnis nicht ueberdecken


if __name__ == "__main__":
    sys.exit(main())
