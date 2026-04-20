Lantmäteriet (LM), Karta 1:10 000 Nedladdning (CC BY 4.0)
link: ftp://download-opendata.lantmateriet.se/

OpenStreetMap contributors, Sweden extract (ODbL 1.0)
link: https://download.geofabrik.de/europe/sweden-latest.osm.pbf
usage: address-geocoding source. Parsed at normalize-time via
  `scripts/fetch_osm.py` into `data/normalized/osm/addresses.parquet`
  (Stockholm kommun bbox only, ~131 k addresses). ODbL requires attribution
  "© OpenStreetMap contributors" on any export derived from this.

Stockholms Stads Stadsbyggnadskontor (SBK), Stadskarta (1:4000 - 1:8000)
metadata: https://dataportalen.stockholm.se/dataportalen/GetMetaDataById?id=0eed76ad-7a89-4da1-9766-e9fcf7b31789
link: https://dataportalen.stockholm.se/dataportalen/Data/Stadsbyggnadskontoret/Stadskarta_hela_Stockholm.zip
produktspecifikation: https://dataportalen.stockholm.se/dataportalen/GetMetaDataById?id=0eed76ad-7a89-4da1-9766-e9fcf7b31789

Statiska centralbyrån (SCB), Demografiska statistikområden (DeSO)
description: https://www.scb.se/vara-tjanster/oppna-data/oppna-geodata/oppna-geodata-for-deso---demografiska-statistikomraden/
Geographic areas (2025): https://geodata.scb.se/geoserver/stat/wfs?service=WFS&REQUEST=GetFeature&version=1.1.0&TYPENAMES=stat:DeSO_2025&outputFormat=geopackage
Geographic areas (2018) https://geodata.scb.se/geoserver/stat/wfs?service=WFS&REQUEST=GetFeature&version=1.1.0&TYPENAMES=stat:DeSO_2018&outputFormat=geopackage
data tables: https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/demografiska-statistikomraden-deso/deso-tabellerna-i-ssd--information-och-instruktioner/
# SCB DeSO – Data sources

Bulk CSV downloads of every table in **Statistikdatabasen (SSD)** that uses the
**Demografiska statistikområden (DeSO)** classification, Sweden's nationwide
small-area demographic grid (~6,160 areas in DeSO 2025).

- **Publisher:** Statistikmyndigheten SCB (Statistics Sweden)
- **Index page:** <https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/demografiska-statistikomraden-deso/deso-tabellerna-i-ssd--information-och-instruktioner/>
- **Format:** ZIP archive containing semicolon-separated CSV (`;`) in Windows-1252 encoding plus a metadata file
- **Bulk URL pattern:** `https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB<id>_sv.zip`
- **License:** Creative Commons CC0 1.0 (SCB open data)
- **Captured:** 2026-04-17
- **DeSO version:** New tables use DeSO 2025; older time series remain on DeSO 2018 and are not back-revised

> **Note:** The numeric `TAB####` IDs are SCB's internal identifiers and may change when a table is republished. Re-scrape the PxWeb table pages periodically to confirm the bulk URLs.

---

## Befolkningens arbetsmarknadsstatus (BAS)

| # | Title | Period | PxWeb table | Bulk CSV (zip) |
|---|---|---|---|---|
| 1 | Arbetsmarknadsstatus efter bostadens belägenhet, region (DeSO/RegSO), kön och ålder | 2020–2024 | [ArRegDesoStatusN](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0210__AM0210G/ArRegDesoStatusN/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6680_sv.zip> |
| 2 | Sysselsatta 15–74 år efter bostadens belägenhet, region (DeSO/RegSO), kön och näringsgren (SNI 2007) | 2020–2024 | [ArRegDesoSNI2007N](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0210__AM0210G/ArRegDesoSNI2007N/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6681_sv.zip> |
| 3 | Sysselsatta 15–74 år efter bostadens belägenhet, region (DeSO/RegSO), kön och sektor | 2020–2024 | [ArRegDesoSektorN](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0210__AM0210G/ArRegDesoSektorN/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6682_sv.zip> |

## Befolkningens utbildning

| # | Title | Period | PxWeb table | Bulk CSV (zip) |
|---|---|---|---|---|
| 4 | Befolkning 25–64 år efter region och utbildningsnivå | 2015–2023 | [UtbSUNBefDesoRegso](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__UF__UF0506__UF0506D/UtbSUNBefDesoRegso/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB5956_sv.zip> |
| 5 | Befolkning 25–65 år efter region och utbildningsnivå | 2024 | [UtbSUNBefDesoRegsoN](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__UF__UF0506__UF0506D/UtbSUNBefDesoRegsoN/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6534_sv.zip> |

## Befolkningsstatistik

| # | Title | Period | PxWeb table | Bulk CSV (zip) |
|---|---|---|---|---|
| 6 | Folkmängden efter region, ålder och kön | Yearly | [FolkmDesoAldKon](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/FolkmDesoAldKon/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6574_sv.zip> |
| 7 | Folkmängden efter region, utländsk/svensk bakgrund och kön | Yearly | [FolkmDesoBakgrKon](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/FolkmDesoBakgrKon/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6571_sv.zip> |
| 8 | Folkmängden efter region, civilstånd och kön | Yearly | [FolkmDesoCivilKon](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/FolkmDesoCivilKon/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6570_sv.zip> |
| 9 | Folkmängden efter region, födelseregion och kön | Yearly | [FolkmDesoLandKon](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/FolkmDesoLandKon/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6572_sv.zip> |
| 10 | Folkmängden efter region, medborgarskap och kön | Yearly | [FolkmDesoMedKon](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/FolkmDesoMedKon/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6569_sv.zip> |
| 11 | Antal hushåll efter region, hushållstyp | Yearly | [HushallDesoTyp](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/HushallDesoTyp/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6568_sv.zip> |

## Bostadsbestånd

| # | Title | Period | PxWeb table | Bulk CSV (zip) |
|---|---|---|---|---|
| 12 | Antal lägenheter efter region (DeSO 2018/RegSO 2020) och upplåtelseform, *not updated* | 2015–2023 | [BO0104T10N](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BO__BO0104__BO0104X/BO0104T10N/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6258_sv.zip> |
| 13 | Antal lägenheter efter region (DeSO/RegSO 2025) och upplåtelseform | 2024 | [BO0104T01N2](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BO__BO0104__BO0104X/BO0104T01N2/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6638_sv.zip> |

## Fordonsstatistik

| # | Title | Period | PxWeb table | Bulk CSV (zip) |
|---|---|---|---|---|
| 14 | Personbilar efter status och region, *not updated* | 2015–2023 | [PersBilarDeso](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__TK__TK1001__TK1001Z/PersBilarDeso/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6091_sv.zip> |
| 15 | Personbilar efter status och region | 2024 | [PersBilarDesoN](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__TK__TK1001__TK1001Z/PersBilarDesoN/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6589_sv.zip> |

## Hushållens ekonomi

| # | Title | Period | PxWeb table | Bulk CSV (zip) |
|---|---|---|---|---|
| 16 | Andel av befolkningen per inkomstklass efter region, inkomstslag och kön | Yearly | [Tab1InkDesoRegso](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0110__HE0110I/Tab1InkDesoRegso/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6679_sv.zip> |
| 17 | Låg respektive hög ekonomisk standard efter region och ålder | Yearly | [Tab4InkDesoRegso](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0110__HE0110I/Tab4InkDesoRegso/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6685_sv.zip> |
| 18 | Ekonomisk standard, andel av befolkningen per inkomstklass efter region | Yearly | [Tab3InkDesoRegso](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0110__HE0110I/Tab3InkDesoRegso/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6684_sv.zip> |
| 19 | Inkomststruktur nettoinkomst efter region och kön | Yearly | [Tab2InkDesoRegso](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0110__HE0110I/Tab2InkDesoRegso/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6683_sv.zip> |

## Hushållens boende

| # | Title | Period | PxWeb table | Bulk CSV (zip) |
|---|---|---|---|---|
| 20 | Antal personer efter region och hustyp | Yearly | [HushallT32Deso](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0111__HE0111YDeSo/HushallT32Deso/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6065_sv.zip> |
| 21 | Antal personer efter region och upplåtelseform | Yearly | [HushallT33Deso](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0111__HE0111YDeSo/HushallT33Deso/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6253_sv.zip> |

## Registerbaserad arbetsmarknadsstatistik (RAMS)

| # | Title | Period | PxWeb table | Bulk CSV (zip) |
|---|---|---|---|---|
| 22 | Förvärvsarbetande nattbefolkning 16+ år efter region och bransch (SNI07) | 2018 | [NattDeSoBra](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoBra/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB660_sv.zip> |
| 23 | Förvärvsarbetande nattbefolkning 16+ år efter region, sektor och kön | 2018 | [NattDeSoSekt](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoSekt/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB682_sv.zip> |
| 24 | Befolkningen 16–64 år efter region, sysselsättning och kön | 2018 | [BefDeSoSyss](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/BefDeSoSyss/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB683_sv.zip> |
| 25 | Befolkningen 16–64 år efter region, sysselsättning och kön, new time series | 2019– | [BefDeSoSyssN](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/BefDeSoSyssN/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB5880_sv.zip> |
| 26 | Förvärvsarbetande nattbefolkning 16–74 år efter region och bransch (SNI07), new time series | 2019– | [NattDeSoBraN](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoBraN/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB5842_sv.zip> |
| 27 | Förvärvsarbetande nattbefolkning 16–74 år efter region, sektor och kön, new time series | 2019– | [NattDeSoSektN](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoSektN/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB5843_sv.zip> |

## Miljö – Land- och vattenarealer

| # | Title | Period | PxWeb table | Bulk CSV (zip) |
|---|---|---|---|---|
| 28 | Land- och vattenareal per den 1 januari efter region och arealtyp | 2025 | [Areal2025](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0802/Areal2025/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6420_sv.zip> |

## Miljö – Bebyggelsestruktur och bostäder

| # | Title | Period | PxWeb table | Bulk CSV (zip) |
|---|---|---|---|---|
| 29 | Bostadsbyggnader efter region och byggnadstyp | 2010–2024 | [Bostadsbyggnad3](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0803__MI0803B/Bostadsbyggnad3/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6620_sv.zip> |
| 30 | Bostadsbebyggelsens ålder efter region | 2010–2024 | [BostadsbyggnadAlder3](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0803__MI0803B/BostadsbyggnadAlder3/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6540_sv.zip> |
| 31 | Byggnader, antal och markyta efter region och byggnadstyp | 2010–2024 | [MarkanvByggnadLnKn3](https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0803__MI0803B/MarkanvByggnadLnKn3/) | <https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6621_sv.zip> |

---

## Download script

```bash
#!/usr/bin/env bash
# Fetch all DeSO bulk CSV-zip files from SCB Statistikdatabasen
set -euo pipefail
mkdir -p scb_deso && cd scb_deso

cat > urls.txt <<'EOF'
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6680_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6681_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6682_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB5956_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6534_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6574_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6571_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6570_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6572_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6569_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6568_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6258_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6638_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6091_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6589_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6679_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6685_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6684_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6683_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6065_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6253_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB660_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB682_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB683_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB5880_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB5842_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB5843_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6420_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6620_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6540_sv.zip
https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6621_sv.zip
EOF

wget -c -i urls.txt
```
