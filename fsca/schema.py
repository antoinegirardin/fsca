"""Schéma de sortie structurée demandé à Claude pour chaque avis de sécurité (FSN).

Référentiels utilisés pour rendre les champs comparables d'une notice à l'autre :
- Actions FSCA / actions utilisateur : rubriques du modèle de FSN de la Commission
  européenne (vigilance MDR/IVDR, "Manufacturer Field Safety Notice template").
- Catégorie de dispositif : niveau 1 de la nomenclature EMDN (European Medical
  Device Nomenclature, lettres A à Z).
- Gravité : échelle qualitative à 5 niveaux usuelle avec ISO 14971.
- Classe de rappel : définitions FDA (21 CFR 7.3(m)) — I = probabilité raisonnable
  de conséquences graves ou de décès, II = conséquences temporaires/réversibles ou
  probabilité faible de conséquences graves, III = conséquences improbables.

Contrainte API : la grammaire compilée à partir du schéma est limitée en taille.
Le modèle répond donc avec des codes courts ASCII (énumérations) et des chaînes
vides plutôt que `null` ; `to_record()` convertit ensuite localement vers les
libellés français et `null`, qui sont la forme stockée et exportée.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Libellés (côté local uniquement, absents du schéma envoyé) -------------

EMDN_LABELS = {
    "A": "A - Dispositifs d'administration, de prélèvement et de collecte",
    "B": "B - Dispositifs d'hématologie et d'hémotransfusion",
    "C": "C - Dispositifs du système cardiocirculatoire",
    "D": "D - Désinfectants, antiseptiques, stérilisants et détergents pour DM",
    "F": "F - Dispositifs de dialyse",
    "G": "G - Dispositifs gastro-intestinaux",
    "H": "H - Dispositifs de suture",
    "J": "J - Dispositifs implantables actifs",
    "K": "K - Dispositifs d'endothérapie et d'électrochirurgie",
    "L": "L - Instruments chirurgicaux réutilisables",
    "M": "M - Dispositifs pour pansements",
    "N": "N - Dispositifs du système nerveux et médullaire",
    "P": "P - Prothèses implantables et dispositifs d'ostéosynthèse",
    "Q": "Q - Dispositifs dentaires, ophtalmologiques et ORL",
    "R": "R - Dispositifs respiratoires et d'anesthésie",
    "S": "S - Dispositifs de stérilisation",
    "T": "T - Équipements de protection du patient et aides pour incontinence",
    "U": "U - Dispositifs du système urogénital",
    "V": "V - Dispositifs médicaux divers",
    "W": "W - Dispositifs médicaux de diagnostic in vitro",
    "X": "X - Produits sans destination médicale (annexe XVI MDR)",
    "Y": "Y - Dispositifs pour personnes handicapées",
    "Z": "Z - Équipements médicaux, accessoires, logiciels et consommables associés",
    "?": "indéterminée",
}

NATURE_LABELS = {
    "md": "dispositif médical",
    "ivd": "DM de diagnostic in vitro",
    "software": "logiciel (DM)",
    "accessory": "accessoire",
    "system_kit": "système ou kit",
    "other": "autre",
}

PORTEE_LABELS = {
    "all": "tous les dispositifs de ce modèle",
    "lots_serials": "lots ou numéros de série spécifiques",
    "sw_versions": "versions logicielles spécifiques",
    "configuration": "selon configuration ou usage",
    "unspecified": "non précisée",
}

ACTION_FSCA_LABELS = {
    "recall": "retour / rappel physique",
    "onsite_fix": "modification ou réparation sur site",
    "sw_update": "mise à jour logicielle",
    "replacement": "remplacement du dispositif",
    "destruction": "destruction",
    "ifu_label_change": "modification des instructions d'utilisation ou de l'étiquetage",
    "use_advice": "conseils sur l'utilisation",
    "user_check": "inspection / vérification par l'utilisateur",
    "patient_followup": "suivi des patients",
    "info_only": "information uniquement",
    "other": "autre",
}

ACTION_USER_LABELS = {
    "identify": "identifier les dispositifs concernés",
    "quarantine": "mettre en quarantaine / cesser d'utiliser",
    "return": "retourner les dispositifs",
    "destroy": "détruire les dispositifs",
    "accept_onsite": "accepter une intervention sur site",
    "install_update": "installer une mise à jour",
    "workaround": "appliquer une mesure de contournement",
    "patient_mgmt": "suivre les recommandations de prise en charge des patients",
    "read_ifu_change": "prendre connaissance de la modification des instructions",
    "forward": "transmettre l'avis aux personnes concernées",
    "ack_form": "renvoyer le formulaire d'accusé de réception",
    "other": "autre",
}

URGENCE_LABELS = {"immediate": "immédiate", "high": "élevée", "moderate": "modérée", "low": "faible"}
GRAVITE_LABELS = {
    "negligible": "négligeable", "minor": "mineure", "serious": "sérieuse",
    "critical": "critique", "catastrophic": "catastrophique", "unknown": "non déterminable",
}
PROBA_LABELS = {
    "frequent": "fréquente", "probable": "probable", "occasional": "occasionnelle",
    "remote": "rare", "improbable": "improbable", "unknown": "non déterminable",
}


# --- Schéma envoyé à l'API ---------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


EMPTY = " Chaîne vide si absent."


class Plage(_Strict):
    type: Literal["lot", "serie"]
    debut: str = Field(description="Première valeur de la plage, telle qu'écrite.")
    fin: str = Field(description="Dernière valeur de la plage, telle qu'écrite.")


class ProduitAssocie(_Strict):
    nom: str = Field(
        description="Nom commercial court du produit, tel qu'il figurerait dans un inventaire "
        "(ex. 'Master Sleeve SLL', 'ICE IH'). Dimensions, bibliothèques et contexte vont dans role."
    )
    fabricant: str = Field(description="Fabricant du produit associé." + " Chaîne vide si absent.")
    references: list[str] = Field(description="Références / numéros d'article, une par entrée.")
    role: str = Field(description="Rôle dans la configuration à risque, ex. 'implant utilisé avec la douille'.")


class Identifiants(_Strict):
    udi_di_gtin: list[str] = Field(description="UDI-DI / GTIN / EAN tels qu'écrits, sans le préfixe '(01)'.")
    basic_udi_di: list[str]
    references_catalogue: list[str] = Field(
        description="REF, numéro d'article / de catalogue, part number, code produit. Une valeur par entrée."
    )
    modeles: list[str] = Field(description="Désignations de modèle / variante / taille distinctes des références.")
    versions_logicielles: list[str] = Field(
        description="Numéros de version seuls ('3.1', '2.3.4', '7.0 SP2'), un par entrée ; les noms de code "
        "('Rijeka') vont dans modeles."
    )
    lots: list[str] = Field(description="Un numéro de lot par entrée.")
    numeros_serie: list[str] = Field(description="Un numéro de série par entrée.")
    plages_ou_motifs: list[str] = Field(
        description="Critères non énumérables et non réductibles à une plage début-fin : 'lots commençant par "
        "A02', 'fabriqués entre 01.2025 et 06.2025', 'UDI-PI (10)A02B03E****'. Pas de texte descriptif."
    )
    plages_numeriques: list[Plage] = Field(
        description="Plages explicites de lots ou de numéros de série ('SN 10230 à 10560')."
    )
    liste_complete: bool = Field(
        description="false si la notice renvoie à une liste non fournie ou si plus de 500 valeurs ont été résumées."
    )


class Dispositif(_Strict):
    nom_commercial: str
    noms_alternatifs: list[str] = Field(
        description="Autres dénominations du même produit présentes dans la notice : nom court, nom de famille, "
        "désignation complète, nom dans une autre langue, ancien nom. Pas de description générique "
        "de catégorie ('Dental treatment application software', 'pompe à perfusion')."
    )
    type_equipement: str = Field(
        description="Type générique court en français pour la recherche en inventaire : 'pompe à perfusion "
        "volumétrique', 'moniteur patient', 'logiciel de planification implantaire', 'réactif IVD'…"
    )
    description: str = Field(description="1-2 phrases en français : ce qu'est le dispositif et à quoi il sert.")
    emdn: Literal["A", "B", "C", "D", "F", "G", "H", "J", "K", "L", "M", "N", "P", "Q", "R", "S", "T", "U",
                  "V", "W", "X", "Y", "Z", "?"] = Field(
        description="Catégorie EMDN niveau 1 : A administration/prélèvement ; B hématologie ; C cardiocirculatoire ; "
        "D désinfectants ; F dialyse ; G gastro-intestinal ; H suture ; J implantables actifs ; K endothérapie/"
        "électrochirurgie ; L instruments chirurgicaux réutilisables ; M pansements ; N système nerveux ; "
        "P prothèses implantables/ostéosynthèse ; Q dentaire/ophtalmo/ORL ; R respiratoire/anesthésie ; "
        "S stérilisation ; T protection patient/incontinence ; U urogénital ; V divers ; W diagnostic in vitro ; "
        "X sans destination médicale ; Y handicap ; Z équipements, logiciels et consommables ; ? indéterminée."
    )
    nature: Literal["md", "ivd", "software", "accessory", "system_kit", "other"]
    classe_reglementaire: str = Field(description="I, Is, Im, IIa, IIb, III ; IVD A-D, si écrite." + EMPTY)
    portee: Literal["all", "lots_serials", "sw_versions", "configuration", "unspecified"]
    identifiants: Identifiants
    non_concernes: str = Field(
        description="Ce que la notice déclare explicitement NON concerné (modèles, lots, versions)." + EMPTY
    )
    produits_associes: list[ProduitAssocie] = Field(
        description="Produits d'autres références ou fabricants, non défectueux eux-mêmes, mais dont l'usage "
        "combiné définit la configuration à risque (implant utilisé avec le logiciel, consommable d'un "
        "analyseur…). Ne pas les mettre dans identifiants."
    )


class ActionDemandee(_Strict):
    type: Literal["identify", "quarantine", "return", "destroy", "accept_onsite", "install_update", "workaround",
                  "patient_mgmt", "read_ifu_change", "forward", "ack_form", "other"]
    detail: str = Field(
        description="Ce qu'il faut faire concrètement, en français. Code 'quarantine' pour toute consigne "
        "de cesser d'utiliser un dispositif, une fonction, un réglage ou une bibliothèque ; 'workaround' "
        "seulement pour une manière alternative de continuer à l'utiliser."
    )
    delai: str = Field(description="Délai tel qu'indiqué ('immédiatement', 'sous 30 jours', date)." + EMPTY)


class Evaluation(_Strict):
    urgence: Literal["immediate", "high", "moderate", "low"] = Field(
        description="immediate = cesser l'utilisation / agir sans délai ; high = sous quelques jours ; "
        "moderate = action planifiée (semaines) ; low = information sans action matérielle."
    )
    gravite: Literal["negligible", "minor", "serious", "critical", "catastrophic", "unknown"] = Field(
        description="Pire conséquence plausible (ISO 14971) : negligible = gêne ; minor = blessure temporaire sans "
        "intervention ; serious = nécessite une intervention médicale ; critical = atteinte permanente ou "
        "menace vitale ; catastrophic = décès."
    )
    probabilite: Literal["frequent", "probable", "occasional", "remote", "improbable", "unknown"] = Field(
        description="Seulement si la notice donne des éléments (taux de défaut, nombre d'incidents), sinon unknown."
    )
    classe_rappel: Literal["I", "II", "III"] = Field(description="Classe de rappel selon les définitions FDA.")
    arret_utilisation_requis: bool
    patients_deja_traites_concernes: bool = Field(
        description="true si des patients déjà traités, implantés ou diagnostiqués peuvent nécessiter un suivi."
    )
    justification: str = Field(description="2-4 phrases en français justifiant les niveaux choisis.")


class NoticeExtraction(_Strict):
    titre: str = Field(description="Titre de la notice, en français.")
    langue_document: Literal["FR", "EN", "DE", "IT", "other"]
    reference_fabricant: str = Field(description="Numéro de FSN / FSCA du fabricant." + EMPTY)
    date_notice: str = Field(description="AAAA-MM-JJ." + EMPTY)
    fabricant: str
    srn_fabricant: str = Field(description="SRN EUDAMED." + EMPTY)
    dispositifs: list[Dispositif] = Field(description="Un élément par produit distinct concerné.")
    synthese: str = Field(
        description="3-6 phrases en français : produit, problème, risque, quoi faire et dans quel délai."
    )
    probleme: str
    cause: str = Field(description="Cause identifiée par le fabricant." + EMPTY)
    risque: str = Field(description="Risque pour le patient, l'utilisateur ou des tiers.")
    risque_declare_fabricant: str = Field(
        description="Niveau de risque tel que formulé par le fabricant (rubrique 'Risque' du modèle de FSN), "
        "recopié." + EMPTY
    )
    probabilite_declaree_fabricant: str = Field(
        description="Probabilité telle que formulée par le fabricant, recopiée." + EMPTY
    )
    incidents_signales: str = Field(
        description="Incidents/plaintes rapportés, ou 'aucun incident signalé' si c'est écrit." + EMPTY
    )
    types_action_fsca: list[Literal["recall", "onsite_fix", "sw_update", "replacement", "destruction",
                                    "ifu_label_change", "use_advice", "user_check", "patient_followup",
                                    "info_only", "other"]]
    actions_utilisateur: list[ActionDemandee]
    actions_fabricant: list[str]
    destinataires: list[str] = Field(description="'utilisateurs', 'matériovigilance', 'distributeurs', 'patients'…")
    accuse_reception_requis: bool
    delai_accuse_reception: str = Field(description=EMPTY.strip())
    contact: str = Field(description="Nom, e-mail, téléphone indiqués." + EMPTY)
    evaluation: Evaluation
    remarques_extraction: str = Field(
        description="Ambiguïtés, contradictions, texte illisible, annexe manquante." + EMPTY
    )


# --- Conversion vers la forme stockée ----------------------------------------


def _none_if_empty(v: str) -> str | None:
    return v.strip() or None


def to_record(ex: NoticeExtraction) -> dict[str, Any]:
    """Forme stockée/exportée : libellés français et `null` pour les champs absents."""
    d = ex.model_dump(mode="json")
    for k in ("reference_fabricant", "date_notice", "srn_fabricant", "cause", "incidents_signales",
              "risque_declare_fabricant", "probabilite_declaree_fabricant",
              "delai_accuse_reception", "contact", "remarques_extraction"):
        d[k] = _none_if_empty(d[k])
    d["types_action_fsca"] = [ACTION_FSCA_LABELS[c] for c in d["types_action_fsca"]]
    for a in d["actions_utilisateur"]:
        a["type"] = ACTION_USER_LABELS[a["type"]]
        a["delai"] = _none_if_empty(a["delai"])
    for dev in d["dispositifs"]:
        dev["categorie_emdn"] = EMDN_LABELS[dev.pop("emdn")]
        dev["nature"] = NATURE_LABELS[dev["nature"]]
        dev["portee"] = PORTEE_LABELS[dev["portee"]]
        dev["classe_risque_reglementaire"] = _none_if_empty(dev.pop("classe_reglementaire"))
        dev["non_concernes"] = _none_if_empty(dev["non_concernes"])
        dev["noms_alternatifs"] = [n.strip() for n in dev["noms_alternatifs"] if n.strip()]
        for prod in dev["produits_associes"]:
            prod["fabricant"] = _none_if_empty(prod["fabricant"])
            prod["references"] = [r.strip() for r in prod["references"] if r.strip()]
        for key in ("udi_di_gtin", "basic_udi_di", "references_catalogue", "modeles", "versions_logicielles",
                    "lots", "numeros_serie", "plages_ou_motifs"):
            dev["identifiants"][key] = [v.strip() for v in dev["identifiants"][key] if v.strip()]
    ev = d["evaluation"]
    ev["urgence"] = URGENCE_LABELS[ev["urgence"]]
    ev["gravite_iso14971"] = GRAVITE_LABELS[ev.pop("gravite")]
    ev["probabilite"] = PROBA_LABELS[ev["probabilite"]]
    ev["classe_rappel_estimee"] = ev.pop("classe_rappel")
    return d
