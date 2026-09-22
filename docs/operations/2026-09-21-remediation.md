# Herstel en acceptatie — 21 september 2026

Status: uitvoering; dit document is geen verklaring dat de acceptatie voltooid is.
**Gepauzeerd op 21 september rond 01:54 UTC om de resterende gebruikerslimiet
te sparen. Hervat vanuit `docs/operations/2026-09-21-handoff.md`.**
De gebruiker heeft na de read-only audit herstel, onderhoudsstilstand en afronding
van de oorspronkelijke plannen opgedragen. `astra-bevindingen.md` bewaart de audit.

## Herstelpunt en stilstand

- Consistente SQLite-back-up vóór mutaties: control-plane
  `/data/maintenance/20260920T235748Z/control-before.db` (0600, directory 0700).
- Alle 15 managed runners waren idle volgens de eigen forge vóór drain.
- Gewenste toestand via RunnerService naar drained, vervolgens stopped.
  Capaciteiten blijven behouden; nul capaciteit zou runners verwijderen.
- Oude macOS-launchd-daemon had `KeepAlive: true` en bleef na drain herstarten.
  Automatisch starten uitgeschakeld; na controle van idle-status unloaded.
- Registraties, caches, werkdirectories en historie blijven behouden.

## Te verifiëren werkpakketten

- [ ] Verse status + onmogelijke nieuwe jobtoewijzing vóór stop/restart/cache.
- [ ] Gefaseerde fleet-recreate, maximaal één vervanging tegelijk, halt bij fout.
- [ ] Werkelijke geheugenlimieten en templatecontrole vóór plaatsing.
- [ ] Fouten per fleet isoleren; controllerlease vernieuwen tijdens lange acties.
- [ ] Dashboard leest en bedient alle runtimes via mTLS-agents.
- [ ] UUID-gekoppelde jobhistorie; bestaande historie idempotent koppelen.
- [ ] Settings voor zes fleets; gewijzigde secrets werkelijk gebruiken.
- [ ] CPU/job/opslagmetingen compleet, gedateerd, zonder blokkerende heartbeat.
- [ ] Opslaggrenzen en veilige cleanup met behoud van actieve jobs.
- [ ] Echte controllerliveness en persistente onderhoudsmodus.
- [ ] Alle agents en unitimages bijgewerkt en versie aantoonbaar.
- [ ] Linuxmigratie van WSL naar Hyper-V met juiste hostbudgetten.
- [ ] Windows- en macOS-templates en appliancebesturing afmaken.
- [ ] Praktijkacceptatie GitHub/Forgejo × Linux/Windows/macOS.
- [ ] Certificaatvernieuwing, CI-controles, actuele runbook/README/acceptatie.
- [ ] Onafhankelijke specificatie- en kwaliteitsreview, daarna hertests.

## Geaccepteerde ontwerpbesluiten

Windows-processen op BEAST-UNIT, de interne Hyper-V-switch met portproxy en
behoud van de bestaande macOS-QEMU-appliance zijn latere vastgelegde keuzes
(ontwerp §20). Ongebruikte volumes worden niet blind verwijderd.

## Uitvoering tot 21 september 2026, 01:13 UTC

- Gebruiker koos expliciet **80 GiB Linux-VM / 32 GiB Docker Desktop**; tien
  GitHub-Linux-runners blijven het uitgangspunt. Voorgesteld agentbudget:
  72 GiB RAM (8 GiB gastmarge), 640 GiB swap, 712 GiB gecombineerde admission.
  Dit is begrensde overboeking, geen garantie dat alle plafonds tegelijk werken.
- `Set-MaintenanceBudget.ps1` uitgevoerd: Windows-runnerservice naar Manual,
  WSL-keepalive-taak disabled, Linux-VM van16 naar80 GiB en8 naar56vCPU.
  Nieuwe afzonderlijke dynamische datadisks: Linux2304GiB, macOS-host768GiB.
  Bestaande boot/checkpointschijven zijn niet handmatig aangepast/verwijderd.
- `.wslconfig` naar32GB. Vorige configuratie in
  `D:\HyperV\runner-platform\stage\maintenance-20260921\wslconfig-before`.
  Docker Desktop gecontroleerd gestopt en herstart; engine29.5.3 en21containers
  draaien weer, ForgejoHTTP200. Immich en spraakdienst na opstart healthy;
  Open WebUI en Stable Diffusion worden nog op volledige bereikbaarheid nagekeken.
- Hyper-V startte de Linux-VM na circa4m32s, met automatische checkpointmerge
  op de achtergrond. Gastconsole toont Linux/systemd-boot; SSH nog niet terug
  op moment van deze notitie. Geen geforceerde poweroff of handmatige merge.
- Dashboard/controller hersteld in persistente maintenance. Image
  `nomercy/runner-dashboard:maintenance-1b91283a7a1d`, sourceSHA256
  `1b91283a7a1d8d6d0a89f8157fefff6ba9f3f4bc87fc44ecac9274f7f8183c57`.
  Publieke en lokaleURL antwoorden302 naar login; controllerheartbeat vers,
  alle15managedrunners actual/desired stopped. Historiebackfill maakte13
  soft-deletedlegacyidentiteiten, zoals het ontwerp voorschrijft.
  Tweede SQLite/composeback-up: `/data/maintenance/20260921-dashboard-restore/`.
- Root heeft daarna drie reviewbevindingen lokaal gerepareerd: maintenance
  tussen acties, onbekende adapterstatus expliciet weigeren, historieherhaling
  op UUID dedupliceren. **Nog niet opgenomen in bovenstaand liveimage.**
- Volledige dashboardregressie:1896pass,6fail,3skip,9xfail. Zes afwijkingen
  onderzocht en hersteld; gerichte hertest312pass. Nieuwe cert/liveness/API37pass,
  echte Chromiumbrowsertest1pass, reviewfixes/historie/reconciler156pass.
  Aantallen overlappen; definitieve suites na alle integraties nog vereist.
- Linux-opslagbackend en readonlyunitimages lokaal gebouwd; echte64MiB-looptest
  op Linux-VM vóór herstart geslaagd: ENOSPC blijft binnen runnerfilesystem,
  host blijft beschrijfbaar, gecontroleerde verwijdering geslaagd.
  Beide images gebouwd als `nomercy/runner-unit-{github,forgejo}:maintenance-20260921`;
  nog geen echte read-only start/registratie/buildacceptatie.
- Oude macOS-guest netjes uitgezet, restart=no; oorspronkelijke93.4GBqcow2
  blijft intact. Nieuwe guesttemplates waren vooraf geïnstalleerd. Afgeleid
  bootimage gemaakt met oorspronkelijkeCmd en cleanupentrypoint:
  `sha256:c44459446c2c3bf686964fa7774844395ab4bed56bc11eea1c8ffd4a90bc4e6b`.
  Nieuwe perUUID-QEMU-pool lokaal206tests; baseclean/migratie/liveacceptatie open.
- Windows-VHDX-backend met ACL/mount/bootstrapguards lokaal geïmplementeerd.
  Beschermde CPU/RAM/affinity-argumenten toegevoegd. Registratie onder de eigen
  servicesid via lokale JSON-IPC wordt nog afgerond; nog niet livegedeployed.

## FFmpeg-bewijs

Commit `NoMercy-Entertainment/nomercy-ffmpeg@4f756e49` van18september begrenst
BUILD_JOBS standaard op12 om OOM in16GiBcgroups te voorkomen, en vermeldt dat
de Windows-crosscompile daardoor ongeveer verdubbelde. Werkelijke joblog gebruikt12.
Run35395394147 startte alle7platformjobs binnen5s: geen runnerwachtrij als
verklaring. Windows-x64 was193.7min; macOS-x64150.2min, macOS-ARM133.6min,
FreeBSD124.7min. Linuxcachehits waren veel korter. Verder zijn grote opeenvolgende
dependencylagen cachegevoelig; registrycache mode=max is te onderzoeken.
De historische1h40baseline is nog niet zelfstandig gereproduceerd.

Bronnen:
- https://github.com/NoMercy-Entertainment/nomercy-ffmpeg/commit/4f756e49
- https://github.com/NoMercy-Entertainment/nomercy-ffmpeg/actions/runs/35395394147

Open: datadisks formatteren/mounten, swap instellen,13Linuxrunners met volumes
enregistraties migreren, Windows/macOS-opslag en agentuitrol, zes-celliveCI,
eindreviews/CIworkflow/runbook/README en volledige acceptatie. De WSL-distro
blijft als herstelbron behouden; definitieve verwijdering volgt de stabiliteitsperiode.
