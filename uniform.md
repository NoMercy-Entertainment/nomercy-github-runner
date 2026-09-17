Werk in de huidige repository:

D:\docker-compose\GithubRunners

Voer een grondige herarchitecturering uit van het volledige runnerplatform.

Dit is niet alleen een wijziging aan het dashboard. Het doel is dat alle runners ook onder de motorkap volgens dezelfde architectuur, provisioningflow, containerstructuur, lifecycle en beheerinterface werken.

Harde doelstelling

Bouw één uniform, Hyper-V-gebaseerd runnerplatform voor de volledige matrix:

| Platform | GitHub Actions | Forgejo Actions |
| -------- | -------------- | --------------- |
| Linux    | verplicht      | verplicht       |
| Windows  | verplicht      | verplicht       |
| macOS    | verplicht      | verplicht       |

Alle zes combinaties moeten vanuit hetzelfde dashboard kunnen worden aangemaakt, geschaald, gestart, gestopt, herstart, gedraind, verwijderd, gemonitord en onderhouden.

GitHub moet dus naast Linux-runners ook volwaardige Windows- en macOS-runners krijgen. Forgejo moet dezelfde drie platformvarianten ondersteunen.

Niet alleen de gebruikersinterface, maar ook de onderliggende werking moet worden gestandaardiseerd.

Niet-onderhandelbare architectuureisen

1. Hyper-V wordt de gemeenschappelijke infrastructuurlaag.

2. WSL en WSL2 mogen geen onderdeel meer zijn van de uiteindelijke productiearchitectuur:
   - geen WSL-distributie als runnerhost;
   - geen WSL-specifieke Docker-engine;
   - geen WSL keepalive-taak;
   - geen WSL-paden in de operationele runnerarchitectuur;
   - geen dashboard dat slechts de Docker-socket van een WSL-engine beheert.

3. Migreer de bestaande GitHub-runners uit WSL naar Hyper-V.

4. Migreer de bestaande native Windows Forgejo-runner naar dezelfde beheerde container/appliance-architectuur.

5. Integreer de bestaande macOS QEMU-omgeving in dezelfde lifecycle en provisioningstructuur. De macOS-runner mag niet langer een aparte read-only “Elsewhere”-runner zijn.

6. GitHub en Forgejo mogen geen afzonderlijke infrastructuurimplementaties krijgen. Het verschil tussen beide moet beperkt blijven tot een provideradapter voor:
   - registratie;
   - deregistratie;
   - API-status;
   - tokens;
   - labels;
   - jobinformatie.

7. Linux, Windows en macOS mogen geen afzonderlijke dashboardimplementaties krijgen. Platformverschillen moeten achter platformimages, bootstrapcode en runtime-adapters worden afgeschermd.

Gewenste uniforme infrastructuur

Ontwerp één centrale Hyper-V-control-plane met platformworkers.

De doelstructuur moet conceptueel als volgt zijn:

Hyper-V-host
├── management/control-plane
├── Linux worker-VM
│   ├── container-engine
│   ├── GitHub Linux runner-containers
│   ├── Forgejo Linux runner-containers
│   └── eventueel beheerde macOS/QEMU runner-containers
├── Windows worker-VM
│   ├── Windows container-engine
│   ├── GitHub Windows runner-containers
│   └── Forgejo Windows runner-containers
└── macOS runner-appliances
    ├── geïsoleerde macOS-runtime
    ├── GitHub macOS runner-instances
    └── Forgejo macOS runner-instances

Onderzoek of de bestaande macOS QEMU-container binnen de Linux Hyper-V-worker als gestandaardiseerde macOS runner-appliance kan worden gebruikt.

Een letterlijke gedeelde containerimage voor Linux, Windows en macOS is niet vereist, omdat deze platformen verschillende kernels nodig hebben. Wel is verplicht dat iedere runner dezelfde logische runnerstructuur en hetzelfde lifecycle-contract gebruikt.

Iedere runner-instance moet dezelfde onderdelen hebben:

- een declaratieve RunnerSpec;
- een provider: `github` of `forgejo`;
- een platform: `linux`, `windows` of `macos`;
- een architectuur, bijvoorbeeld `x64` of `arm64`;
- een platformimage of appliance-template;
- één geïsoleerde runner-agent;
- een eigen persistente registratiestatus;
- een eigen workspace;
- een eigen cache- en opslaggebied;
- eigen resourcegrenzen;
- dezelfde control-agent;
- dezelfde health-, log- en metricsinterface;
- dezelfde lifecycle-state-machine;
- dezelfde dashboardacties.

Alleen het platformimage en de provideradapter mogen verschillen.

Gewenste provisioningflow

Iedere nieuwe runner moet via exact dezelfde generieke flow worden aangemaakt:

1. De gebruiker kiest in het dashboard:
   - GitHub of Forgejo;
   - Linux, Windows of macOS;
   - aantal instances;
   - labels;
   - runnergroep;
   - CPU;
   - geheugen;
   - schijfruimte;
   - cachebeleid.

2. Het dashboard maakt een declaratieve RunnerSpec.

3. De centrale controller kiest de juiste Hyper-V-worker en platformtemplate.

4. De runtime maakt de runner-instance en geïsoleerde opslag aan.

5. De provideradapter haalt een tijdelijk registratietoken op.

6. De runner wordt geregistreerd bij GitHub of Forgejo.

7. De control-agent rapporteert status, capabilities, logs en telemetrie.

8. De controller verifieert dat de runner online en inzetbaar is.

9. Bij een fout wordt de volledige gedeeltelijke creatie veilig teruggedraaid.

Deze flow mag niet opnieuw afzonderlijk worden geïmplementeerd voor iedere provider of ieder platform.

RunnerSpec en gewenste toestand

Introduceer één generiek, persistent runnermodel, bijvoorbeeld:

- runner_id;
- display_name;
- provider;
- platform;
- architecture;
- runtime_template;
- host_id;
- labels;
- runner_group;
- cpu_limit;
- memory_limit;
- disk_limit;
- cache_policy;
- desired_state;
- actual_state;
- registration_id;
- registration_uuid;
- created_at;
- last_seen_at;
- current_operation;
- last_error.

Gebruik stabiele IDs. Gebruik runnernamen nooit als enige technische identiteit, vooral omdat Forgejo-runnernamen niet uniek hoeven te zijn.

Werk met desired state en reconciliation. Het dashboard bepaalt bijvoorbeeld dat een fleet vijf Windows GitHub-runners moet bevatten; de controller zorgt vervolgens idempotent dat vijf gezonde instances bestaan.

Uniform lifecycle-contract

Alle runners moeten dezelfde lifecycle ondersteunen:

- create;
- provision;
- register;
- start;
- stop;
- restart;
- drain;
- cancel drain;
- recreate;
- remove;
- deregister;
- scale up;
- scale down;
- fetch status;
- fetch logs;
- inspect resources;
- clear cache;
- repair of reconcile.

Implementeer deze acties via één generieke service en één API-contract.

Voorkom verspreide constructies zoals:

- `if platform == windows` in dashboardtemplates;
- aparte action-routes voor externe runners;
- aparte knoppen voor “Elsewhere”;
- hardcoded runnernamen;
- hostspecifieke uitzonderingen in de UI;
- één lifecycle via Docker en een volledig andere lifecycle via losse scripts.

Platformafhankelijke implementaties horen uitsluitend achter duidelijke adapters.

Container- en appliance-isolatie

Iedere runner moet een eigen geïsoleerde uitvoereenheid hebben.

Onderzoek en kies één consistente strategie:

- één runnercontainer per runner-instance binnen een platformworker; of
- één lichte Hyper-V-runner-appliance per runner-instance wanneer containerisolatie technisch onvoldoende is.

Een gedeelde host of worker mag nooit betekenen dat runners dezelfde schrijfbare workspace, registratiedata of onbeperkte cache delen.

Iedere runner krijgt minimaal:

- eigen workspace;
- eigen cacheopslag;
- eigen registratieconfiguratie;
- eigen logs;
- eigen resourcegrenzen;
- eigen lifecycle;
- eigen healthstatus;
- voorspelbare cleanup.

Voor Windows moeten Windows-containers met passende Hyper-V-isolatie worden onderzocht en gebruikt wanneer dat technisch geschikt is.

Voor macOS moet de bestaande QEMU-constructie zorgvuldig worden onderzocht. Als een macOS-runner niet als conventionele container kan draaien, moet deze als een gestandaardiseerde runner-appliance worden behandeld die exact hetzelfde control-protocol en lifecycle-contract aanbiedt.

Noem een VM of QEMU-guest niet ten onrechte een container. Uniformiteit moet uit het contract en de provisioningstructuur komen, niet uit misleidende terminologie.

Control-plane en agents

Het huidige dashboard beheert vooral containers via één lokale Docker-socket. Dat model is onvoldoende voor meerdere Hyper-V-workers.

Ontwerp daarom:

- een centrale controller;
- een inventory van Hyper-V-workers;
- een beveiligde control-agent per worker;
- één versieerbaar control-protocol;
- capability- en healthrapportage;
- asynchrone, traceerbare operaties;
- reconciliation tussen desired en actual state.

De control-agent moet uitsluitend vooraf gedefinieerde runneroperaties accepteren. Geen endpoint voor willekeurige PowerShell-, shell- of SSH-commando’s.

Vereisten:

- sterke onderlinge authenticatie;
- versleuteld verkeer;
- autorisatie per operatie;
- secret-redaction;
- idempotency keys;
- operation IDs;
- time-outs;
- retries met begrenzing;
- auditlogging;
- veilige foutafhandeling;
- versiecompatibiliteit;
- heartbeat en last-seen;
- geen secrets in browserresponses.

De huidige `runner_exporter.py` is read-only. Breid deze niet achteloos uit met onbeveiligde schrijfacties. Bepaal of een aparte control-agent nodig is en implementeer de veiligste onderhoudbare oplossing.

Dashboard

Verwijder het architectuurverschil tussen lokale runners en “Elsewhere”-runners.

Er moet uiteindelijk één generieke runnerkaart en runnerdetailpagina zijn. Iedere kaart toont:

- provider;
- platform;
- architectuur;
- worker/host;
- runtime;
- status;
- actieve job;
- CPU;
- geheugen;
- runneropslag;
- cachegebruik;
- bereikbaarheid;
- laatste heartbeat;
- actuele beheeroperatie;
- foutstatus.

Iedere runner moet dezelfde acties aanbieden:

- drain;
- start;
- stop;
- restart;
- recreate;
- remove;
- logs;
- clear cache.

Fleetniveau moet voor iedere combinatie beschikbaar zijn:

- runner toevoegen;
- gewenste capaciteit instellen;
- scale up;
- scale down;
- hele fleet gecontroleerd recreëren;
- cache van idle runners opschonen.

Maak zes configureerbare fleets:

- GitHub Linux;
- GitHub Windows;
- GitHub macOS;
- Forgejo Linux;
- Forgejo Windows;
- Forgejo macOS.

Cachebeheer

Implementeer één generieke cacheactie met platformspecifieke adapters.

De actie moet per runner uitsluitend data verwijderen waarvan bewezen is dat die runner eigenaar is, bijvoorbeeld:

- Docker/BuildKit-cache;
- ongebruikte runnerimages;
- workspacecache;
- toolcache;
- tijdelijke buildbestanden;
- platformspecifieke caches.

Verwijder nooit een gedeelde hostcache zonder expliciete eigendomsregistratie.

Cache clear moet:

- actieve runners overslaan of eerst drain uitvoeren;
- vooraf en achteraf meten;
- vrijgemaakte ruimte rapporteren;
- gedeeltelijke fouten melden;
- idempotent zijn;
- nooit andere runners beschadigen.

Migratie van de huidige omgeving

Maak een gefaseerd migratieplan voor:

1. Bestaande Linux GitHub-runners in WSL.
2. Bestaande Linux Forgejo-containers.
3. De native Windows Forgejo-service op BEAST-UNIT.
4. De huidige macOS Forgejo-runner in de Hyper-V/QEMU-omgeving.
5. Nieuwe Windows- en macOS-runners voor GitHub.
6. Het dashboard en de huidige Docker-socketbesturing.
7. Bestaande runnerregistraties, namen, labels, caches en historie.

Na succesvolle migratie:

- is WSL niet meer nodig voor runners;
- bestaan geen unmanaged native runnerdiensten meer;
- bestaan geen read-only “Elsewhere”-runners meer;
- worden alle zes fleettypen door dezelfde controller beheerd;
- kunnen oude installers en WSL-scripts worden verwijderd of duidelijk als deprecated worden gemarkeerd.

Voorkom tijdens de migratie:

- orphaned registraties;
- dubbele runners;
- verlies van alle capaciteit tegelijk;
- verlies van historie;
- onbedoeld verwijderen van caches;
- het afbreken van actieve jobs;
- het wijzigen van live infrastructuur zonder expliciete toestemming.

Verplicht vooronderzoek

Analyseer minimaal:

- `dashboard/app.py`;
- `dashboard/docker_ops.py`;
- `dashboard/providers.py`;
- `dashboard/external_telemetry.py`;
- `dashboard/templates/index.html`;
- `dashboard/templates/runner.html`;
- `exporters/`;
- `install/`;
- `docker-compose.runners.yml`;
- `scripts/`;
- alle relevante documenten onder `docs/`.

Onderzoek daarnaast met primaire documentatie:

- Hyper-V-automatisering;
- Linux worker-VMs op Hyper-V;
- Windows-containers met Hyper-V-isolatie;
- containerengineondersteuning op Windows;
- nested virtualization voor de macOS/QEMU-omgeving;
- GitHub runnerondersteuning voor Linux, Windows en macOS;
- Forgejo runnerondersteuning per platform;
- macOS-virtualisatiebeperkingen;
- Apple-licentievoorwaarden voor de bestaande macOS-opstelling.

De eis “Hyper-V voor alles” is hard voor de infrastructuurlaag. Wanneer macOS op de huidige hardware niet technisch, juridisch of betrouwbaar als Hyper-V/QEMU-workload kan worden aangeboden, rapporteer dit expliciet met bewijs. Verzin geen schijnoplossing en claim geen ondersteuning die niet bestaat. Ontwerp in dat geval dezelfde runner-appliance en control-plane voor een compliant Apple-host, terwijl de overige platformen onder Hyper-V blijven.

Implementatievolgorde

1. Documenteer de huidige architectuur.
2. Maak een capability- en migratiematrix.
3. Ontwerp de definitieve Hyper-V-doelarchitectuur.
4. Definieer RunnerSpec, lifecycle-state-machine en control-protocol.
5. Implementeer de centrale controller en workeragent.
6. Migreer de bestaande Linux-containers achter de nieuwe interface.
7. Bouw de Linux Hyper-V-worker.
8. Bouw de Windows Hyper-V-worker en Windows runnerimage.
9. Integreer de macOS runner-appliance.
10. Voeg GitHub- en Forgejo-provideradapters toe aan alle drie platformen.
11. Migreer dashboard en API naar het generieke runnermodel.
12. Implementeer provisioning, scaling, lifecycle, logs en cachebeheer.
13. Voeg veilige migratietools toe.
14. Werk installers, configuratie en documentatie bij.
15. Test alle zes combinaties.

Stop niet na alleen een analyse of ontwerp. Ga door met implementeren wat veilig lokaal kan worden gebouwd en getest. Wijzig geen actieve Hyper-V-VMs, live runners, registraties of productieconfiguratie zonder expliciete toestemming.

Acceptatiecriteria

De opdracht is pas gereed wanneer:

- alle zes provider/platformcombinaties zijn geïmplementeerd;
- GitHub werkende Linux-, Windows- en macOS-runners heeft;
- Forgejo werkende Linux-, Windows- en macOS-runners heeft;
- Hyper-V de gemeenschappelijke infrastructuurlaag is;
- WSL geen onderdeel meer is van de runnerarchitectuur;
- alle runners dezelfde RunnerSpec en lifecycle-state-machine gebruiken;
- alle runners via hetzelfde control-protocol worden beheerd;
- alle runners dezelfde geïsoleerde opslagstructuur gebruiken;
- alle runners via dezelfde API en dashboardcomponenten worden bediend;
- iedere fleet vanuit het dashboard kan worden opgeschaald en afgeschaald;
- start, stop, restart, drain, remove, recreate en cache clear voor alle runnerklassen functioneren;
- externe/read-only “Elsewhere”-runners niet meer bestaan;
- providerlogica niet is vermengd met platform- of infrastructuurlogica;
- fouten geen halve instances of verweesde registraties achterlaten;
- bestaande historie behouden blijft;
- secrets nergens worden gelekt;
- regressietests slagen;
- platformintegratietests aantoonbaar zijn uitgevoerd waar de infrastructuur beschikbaar is;
- niet-uitgevoerde tests eerlijk als niet uitgevoerd worden vermeld.

Vereiste eindrapportage

Lever aan het einde:

- de current-state-analyse;
- de definitieve Hyper-V-architectuur;
- een diagram van control-plane, workers en runner-instances;
- de matrix GitHub/Forgejo × Linux/Windows/macOS;
- alle gewijzigde bestanden;
- de migratiestappen vanaf WSL en de bestaande externe runners;
- de uitgevoerde tests en resultaten;
- aangetoonde platform- of licentiebeperkingen;
- handmatige installatie- en deploymentstappen;
- resterende risico’s of openstaande werkzaamheden.

Een cosmetisch uniform dashboard boven verschillende onbeheerde implementaties voldoet uitdrukkelijk niet. De onderliggende provisioning, lifecycle, isolatie, opslag, scaling en communicatie moeten daadwerkelijk via één gedeelde architectuur verlopen.