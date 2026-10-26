# pyxida

Plugins του GitHub Copilot για την καθημερινή έρευνα: **νόμοι** και **αγορές**.

| Plugin | Χρήση | Τι κάνει |
| --- | --- | --- |
| [**nomos**](plugins/nomos/README.md) | `/nomos Τι ισχύει για την άδεια πατρότητας;` | Έρευνα ελληνικής νομοθεσίας με επαληθευμένες παραπομπές σε ΦΕΚ |
| [**agora**](plugins/agora/README.md) | `/agora Ποια καφετιέρα να πάρω;` | Έρευνα αγοράς: ποιο προϊόν να αγοράσεις, με τιμές Skroutz και σταθμισμένη σύγκριση |

## Εγκατάσταση

Χρειάζεσαι το GitHub Copilot CLI ή την εφαρμογή GitHub Copilot.

```shell
copilot plugin marketplace add fotis89/pyxida
copilot plugin install nomos@pyxida
copilot plugin install agora@pyxida
```

Εγκατάστησε όποια από τα δύο θέλεις. Μετά **άνοιξε νέα συνεδρία**. Έλεγχος: `copilot plugin list`.

Το **nomos** χρειάζεται επιπλέον Python 3.9+ και `pip install pymupdf`.

### Αυτόματη ενημέρωση

Στο `~/.copilot/settings.json` πρόσθεσε `"autoUpdate": true` στην καταχώριση του marketplace:

```json
"extraKnownMarketplaces": {
  "pyxida": {
    "source": { "source": "github", "repo": "fotis89/pyxida" },
    "autoUpdate": true
  }
}
```

Η αυτόματη ενημέρωση γίνεται σε διαδραστικές συνεδρίες του CLI και με `-p`. Αλλιώς ενημερώνεις με το χέρι: `copilot plugin update nomos`.

### Μετάβαση από το παλιό `nomos@nomos`

Αν είχες εγκαταστήσει το nomos από το παλιό marketplace `nomos`:

```shell
copilot plugin uninstall nomos
copilot plugin marketplace remove nomos
```

και μετά ακολούθησε την εγκατάσταση παραπάνω.

## Δομή

```
.github/plugin/marketplace.json   λίστα των plugins
plugins/nomos/                    νομική έρευνα (skills, agents, MCP server για ΦΕΚ)
plugins/agora/                    έρευνα αγοράς (skill)
```

## Άδεια

[MIT](LICENSE). Τα plugins κάνουν έρευνα και **δεν** παρέχουν νομική ή επαγγελματική συμβουλή.
