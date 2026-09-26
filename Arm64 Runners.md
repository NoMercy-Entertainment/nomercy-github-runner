Dat is een beest van een processor. Toch moet ik een technische illusie doorprikken: **jouw 56 cores gaan dit specifieke emulatieprobleem niet oplossen.**

QEMU's vertalingsengine (TCG) schaalt namelijk niet efficiënt over tientallen cores. Eén virtuele ARM-core wordt in software vertaald door exact één fysieke core op jouw systeem. Wat wél in je voordeel werkt, is die **2,8 GHz**. Omdat emulatie zwaar leunt op de brute rekenkracht per individuele core (single-thread prestaties), zorgt die hoge kloksnelheid ervoor dat Windows ARM64 bij jou aanzienlijk beter zal draaien dan bij de meeste anderen.

Je kunt de virtuele machine in de configuratie meerdere cores toewijzen (bijvoorbeeld 8), waardoor 8 van jouw 56 cores aan het werk worden gezet. De overige 48 cores zullen tijdens dit proces uit hun neus eten.

Als je dit wilt aanzwengelen, is dit de basisopzet om al die rekenkracht aan het werk te zetten.

### 1. Wat je moet downloaden

* **Windows ARM64-image:** Download een kant-en-klare `.VHDX` (via het Windows Insider programma) of bouw een ISO via *UUP dump*.
* **ARM UEFI Firmware (QEMU_EFI.fd):** Deze heb je nodig om Windows te laten booten. Op Linux vind je deze vaak door het pakket `qemu-efi-aarch64` of `edk2-aarch64` te installeren. Op Windows kun je deze online als los bestand vinden.

### 2. De QEMU-configuratie

Wanneer je je VHDX en je `QEMU_EFI.fd` in dezelfde map hebt staan, gebruik je de volgende parameters om een virtuele machine met 8 cores en 8GB RAM te starten (pas de bestandsnamen aan naar jouw situatie):

```bash
qemu-system-aarch64 \
  -M virt \
  -cpu max \
  -smp 8 \
  -m 8192 \
  -bios QEMU_EFI.fd \
  -device ramfb \
  -device qemu-xhci \
  -device usb-kbd \
  -device usb-tablet \
  -drive if=none,file=windows_arm64.vhdx,id=hd0 \
  -device virtio-blk-device,drive=hd0

```

**Wat deze parameters doen:**

* `-M virt` en `-cpu max`: Vertelt QEMU om een generiek ARM-moederbord te simuleren en de meest geavanceerde virtuele ARM-processor te gebruiken die het kan bedenken.
* `-smp 8`: Hier wijs je 8 virtuele cores toe (die dus 8 van jouw 56 fysieke cores voluit laten stampen).
* `-device ramfb`: Zorgt voor een simpele, compatibele virtuele videokaart zodat je de grafische interface van Windows daadwerkelijk te zien krijgt.
* `-device virtio-blk-device`: Koppelt jouw virtuele harde schijf via een efficiënte interface om de opslagsnelheid te maximaliseren.

Zodra je dit draait, zul je het Windows-installatiescherm of opstartscherm zien. Het zal even duren, maar met 2,8 GHz kom je er zonder twijfel doorheen.