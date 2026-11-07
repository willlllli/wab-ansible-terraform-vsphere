# Terraform vs. Ansible: Benchmark auf on-premise vSphere

Code, Konfiguration und Rohdaten zur wissenschaftlichen Ausarbeitung „Terraform vs. Ansible: Ein Benchmark-Vergleich zur Provisionierung von Windows-Server-VMs in on-premise vSphere-Umgebungen“ (Anton Reinicke, Provadis School of International Management and Technology, 2026).

Gemessen wurden Deployment-Zeit, Reset-Zeit und Fehlerrate beider Werkzeuge beim Klonen von 1, 2, 4, 8, 16 und 32 Windows-Server-2022-VMs gleichzeitig aus derselben Vorlage, je 30 Zyklen pro Werkzeug und Stufe.

## Stände

| Tag / Branch | Inhalt |
|---|---|
| `vorlauf` | Code-Stand des Vorlaufs |
| `hauptlauf` | **gemessener Code-Stand des Hauptlaufs** (Wrapper 1.3) |
| `haupt-daten` | Rohdaten nach Ende des regulären Hauptlaufs |
| `haupt-daten-2` | **Rohdaten der Auswertung**, nach Nachholung von Zyklus 178 |
| `main` | Wrapper 1.4: nachträgliche Korrektur dreier Mängel (Nachholen nach Halt, Zuordnung von Resten, verwaiste Kindprozesse), ohne Änderung von Messablauf oder Zeitmessung. Mit 1.4 wurde nicht gemessen. |

## Aufbau

```
terraform/      Terraform-Konfiguration (Klon, Customization, Reset über destroy)
ansible/        deploy.yml, cleanup.yml, select_slots.yml (gemeinsames Vor-Play)
scripts/        wrapper.py (Messablauf), validate_vms.py (werkzeugneutrale Validierung),
                compare_vms.py (Konfigurationsvergleich der Klone), create_slot_specs.py,
                vlan_subnets.py, topology.py, start_wrapper.sh
results/        zyklen_<phase>.csv (eine Zeile je Zyklus), vms_<phase>.csv (eine Zeile je VM)
inventory.yaml.example   Struktur der gemeinsamen Parameterdatei beider Werkzeuge
wab-setup/requirements.yaml   Collections für die Offline-Installation
```

Ein Zyklus besteht aus `precheck` (Zielordner leer) → `deploy` → `validate` → `reset`. Die Zeiten misst der Wrapper mit monotoner Uhr um den jeweiligen Werkzeugaufruf. Der Parallelitätsgrad wird je Stufe auf p gesetzt (`terraform apply/destroy -parallelism=p`, `ansible-playbook -f p`). Die Validierung prüft je VM über die vCenter-API Existenz, Power-Status, VMware Tools, Slot-Adresse und Hostname.

## Versionen der Messung

| Komponente | Version |
|---|---|
| Terraform / Provider `vmware/vsphere` | 1.16.3 / 2.15.2 (in `terraform/versions.tf` gepinnt) |
| ansible-core / `community.vmware` / `vmware.vmware` | 2.20.8 / 6.2.1 / 2.10.0 |
| Python / pyVmomi | 3.12 / 9.1.1.0 |
| vCenter / ESXi | 7.0.3 / 6.5.0 |
| Gast | Windows Server 2022 Standard (Desktop Experience), VMware Tools 12.5.4 |

## Nutzung

Nicht enthalten sind die Parameterdatei mit den Objektnamen der Laborumgebung (`inventory.yaml`), die Zugangsdaten (`secrets.env`), Terraform-State und die Logs je Zyklus.

1. `inventory.yaml.example` nach `inventory.yaml` kopieren und die Platzhalter `<…>` durch die eigenen vCenter-Objekte ersetzen. Terraform, Ansible und die Python-Skripte lesen alle Parameter aus dieser einen Datei.
2. `secrets.env` mit den Zugangsdaten anlegen (`chmod 600`). Sie muss die Umgebungsvariablen `VSPHERE_*` für Terraform und `VMWARE_*` für Ansible setzen.
3. Python-venv `venv-wab/` mit `pyvmomi`, `pyyaml`, `requests` und `ansible-core` anlegen, Collections aus `wab-setup/requirements.yaml` installieren, `terraform init` in `terraform/`.
4. Ablauf über den Wrapper:

```bash
scripts/start_wrapper.sh plan --phase haupt --runden 30 --seed <seed>
scripts/start_wrapper.sh run --phase haupt      # abarbeiten bzw. fortsetzen
scripts/start_wrapper.sh status
```

Anhalten nach dem laufenden Zyklus mit `touch run/STOP`.

**Wichtig:** Der Reset löscht VMs. Gelöscht wird ausschließlich über die explizite Namensliste der Zyklus-VMs (`wab-tf-nn`, `wab-an-nn`) im Zielordner, nie über Muster. Trotzdem nur in einer dedizierten Testumgebung einsetzen. Die Zertifikatsprüfung ist auf beiden Seiten deaktiviert. Getestet wurde ausschließlich mit den oben genannten Versionen.

## Lizenz

Apache License 2.0, siehe [LICENSE](LICENSE).
