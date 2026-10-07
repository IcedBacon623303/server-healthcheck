# Server Healthcheck

**Linuxi serveri kontrollimise tööriist** — Henri Haug · ITS24 · VOCO

Väikese IT-meeskonna igapäevane küsimus: kas teenus töötab, kettal on ruumi ja viimane varundus on olemas? See tööriist teeb need kontrollid ühe seadistusfaili järgi ning koostab inimesele loetava Markdowni raporti ja masinloetava JSONi.

Projekt ühendab süsteemihalduse, võrgudiagnostika, programmeerimise ja automaatse testimise. Rakendus kasutab Python 3.11+ standardteeki; lisapakette pole vaja.

## Kiire proovimine

```bash
git clone https://github.com/IcedBacon623303/server-healthcheck.git
cd server-healthcheck
python3 -m unittest discover -s tests -v
python3 demo.py
```

Demo käivitab kohaliku HTTP-teenuse juhuslikul vabal pordil ja loob ajutise varundusfaili. Seejärel kontrollib kolme päris olukorda:

| Katse | Veebiteenus | Varundus | Tulemus |
|---|---|---|---|
| Töötav süsteem | HTTP 200 | Värske, sisuga fail | OK |
| Rikke tekitamine | HTTP 503 | 72 tundi vana fail | CRITICAL |
| Taastamine | HTTP 200 | Uuesti värske fail | OK |

TCP-port jääb rikke katses avatuks. See näitab, miks avatud port üksi ei kinnita rakenduse korrasolekut: HTTP-kontroll leiab rikke eraldi. Raportid tekivad `evidence/` kausta. Teenus suletakse ja ajutised andmed eemaldatakse ka vea korral. Demo ei võta ühendust väliste serveritega.

## Kontrollid

| Kontroll | Mida mõõdab | Häire |
|---|---|---|
| `disk` | Ketta kasutusprotsent ja vaba ruum | Seadistatavad hoiatus- ja kriitiline piir |
| `service` | Ühe systemd teenuse tegelik olek | Mitteaktiivne teenus on CRITICAL; kättesaamatu olek UNKNOWN |
| `tcp` | Kas sihtpordiga saab ühenduse luua | Ühenduse viga või aegumine |
| `http` | Kas tervise URL vastab oodatud HTTP koodiga | Vale kood, võrgurike või vigane TLS sertifikaat |
| `backup` | Täpse varundusfaili olemasolu, suurus ja vanus | Puuduv/tühi fail või liiga vana varundus |

Varunduse kontroll mõõdab faili värskust. Taastamise kontrollimiseks tuleb varukoopia eraldi taastada ja andmete sisu kontrollida. Tuleviku ajatempel annab UNKNOWN, et kellaviga ei näiks värske varundusena.

## Oma serveri seadistamine

```bash
cp config.example.toml config.local.toml
# Muuda teenusenimed, aadressid ja failide teed oma serverile sobivaks.
python3 healthcheck.py --config config.local.toml --output reports
echo $?
```

Näidis kasutab Nginxi, PostgreSQLi ja andmebaasi varundusfaili. Need on seadistusnäited, mitte selle hoidla paigaldatavad teenused. Nginxi `/health` peab tagastama HTTP 200. Vaikimisi tehakse kuni neli kontrolli korraga; tulemuste järjekord jääb seadistusfaili järjekorraks.

| Olek | Väljumiskood | Tähendus |
|---|---:|---|
| OK | 0 | Kõik kontrollid korras |
| WARN | 1 | Vähemalt üks hoiatus |
| CRITICAL | 2 | Vähemalt üks rike, UNKNOWN puudub |
| UNKNOWN | 3 | Kontrolli ei saanud usaldusväärselt teha või seadistus/raporti kirjutamine ebaõnnestus |

Kui raportis on korraga CRITICAL ja UNKNOWN, on koondolek UNKNOWN; konkreetsed rikked jäävad kontrollide tabelisse nähtavaks. Seetõttu loe automatiseerimisel ka JSONi `checks` välja.

`timeout_seconds` piirab võrguoperatsiooni ja systemctl protsessi ootamist. OS-i DNS-lahendus ja kohaliku failisüsteemi päringud ei ole kõva üldtähtajaga piiratud. Kuni 16 töötegijat ja 100 kontrolli väldivad juhuslikult liiga suuri käivitusi.

Raport kirjutatakse ajutisse faili ja asendatakse valmis kujul, nii et pooleli kirjutamist lugejale ei avaldata. JSONi ja Markdowni failid vahetatakse eraldi; nende UTC ajatempel näitab, kas need pärinevad samast käivitusest.

## Automaatne käivitamine Linuxis

`deploy/` sisaldab systemd teenuse ja viieminutilise taimeri näidiseid.

1. Loo süsteemikasutaja `healthcheck` ja paiguta kood `/opt/server-healthcheck` alla.
2. Paiguta kohandatud seadistus `/etc/server-healthcheck.toml` faili.
3. Anna kasutajale seadistuse ja valitud varundusfaili lugemisõigus. Varundust hoia `/srv/backups` all või kohanda kaitsepiire teadlikult.
4. Kopeeri mõlemad unit-failid `/etc/systemd/system` alla ja käivita:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now server-healthcheck.timer
systemctl list-timers server-healthcheck.timer
journalctl -u server-healthcheck.service -n 30
```

Teenusefail kasutab eraldi kasutajat, kaitstud süsteemifaile ja ainult raportikausta kirjutusõigust. `ProtectHome=true` tõttu ei saa see lugeda kodukataloogi varundusi. WARN, CRITICAL ja UNKNOWN kajastuvad oneshot-teenuse ebaõnnestunud olekuna ning raport säilitab täpse põhjuse. Paigaldusnäidist pole sinu arvutis süsteemiteenusena aktiveeritud.

## Kuidas kontrollitud

17 automaatset testi koos alamkatsetega katavad päris HTTP 200/503 vastuseid, avatud ja suletud TCP-porti, kettaruumi, varunduse vanust, CLI väljumiskoode, raporti kirjutamist, piirväärtusi, vigast seadistust, aegumist ja systemd olekuid. Systemd olekute ühiktestid kasutavad kontrollitud protsessivastuseid; võrgu- ja failikatsed kasutavad päris kohalikku teenust ning faile.

GitHub Actions käivitab testid ja kolme-etapilise demo Ubuntu peal Python 3.11 ja 3.13-ga. Kui jooksval masinal on systemd, kontrollib demo ka päris `dbus.service` teenust. Iga CI käivitus salvestab oma raportid allalaaditavate artefaktidena. Hoidlas olevad [töötava süsteemi](evidence/healthy.md), [rikke](evidence/outage.md) ja [taastamise](evidence/recovered.md) raportid pärinevad kohalikust päriselt käivitatud demost.

## Tehnilised valikud

- Seadistus on TOML; tundmatud võtmed, korduvad kontrollinimed ja sobimatud piirid lükatakse tagasi.
- Teenuse kontroll kasutab argumentide loendit ja `shell=False`. Seadistus ei luba suvaliste käskude käivitamist.
- HTTPS kontrollib sertifikaadi usaldust ja hostinime. Ümbersuunamisi automaatselt ei järgita.
- HTTP kontroll ei laadi vastuse sisu ega lisa autentimisandmeid. URL-is pole lubatud paroole ega päringuparameetreid.
- Tööriist jälgib ja raporteerib; teenuste parandamine jääb administraatori otsuseks.

## Projektiga vestluseks valmistumine

Proovi ise muuta üks HTTP vastus 503-ks ja üks varundus vanaks. Seejärel selgita raporti abil, miks avatud TCP-port ei tähenda töötavat veebirakendust, miks UNKNOWN erineb rikkest ning miks värske varundusfail vajab eraldi taastamiskatset.

Kood, dokumentatsioon ja testid on koostatud Codexi abiga. Projekt on õppelabori tööriist, mida saab iseseisvalt käivitada ja edasi arendada.

Tehnilised viited: [systemctl](https://www.man7.org/linux/man-pages/man1/systemctl.1.html), [Python subprocess](https://docs.python.org/3/library/subprocess.html), [TLS kontroll](https://docs.python.org/3/library/ssl.html).
