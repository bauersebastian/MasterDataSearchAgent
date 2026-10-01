sap.ui.define([
  "sap/ui/core/mvc/Controller",
  "sap/ui/core/routing/History",
  "sap/ui/model/json/JSONModel",
  "sap/m/MessageBox",
  "sap/m/MessageToast",
  "sap/m/Label",
  "sap/m/Text",
  "fis/mdsa/controller/api"
], (Controller, History, JSONModel, MessageBox, MessageToast, Label, Text, api) => {
  "use strict";

  // German labels of the MARA fields (the rest is shown with its technical name)
  const MARA_LABELS = {
    MATNR: "Materialnummer", MTART: "Materialart", MBRSH: "Branche", MATKL: "Warengruppe", MEINS: "Basismengeneinheit",
    EAN11: "EAN/UPC", NUMTP: "EAN-Typ", BRGEW: "Bruttogewicht", NTGEW: "Nettogewicht", GEWEI: "Gewichtseinheit",
    BISMT: "Alte Materialnummer", SPART: "Sparte", PRDHA: "Produkthierarchie", TRAGR: "Transportgruppe",
    MTPOS_MARA: "Allg. Positionstypengruppe", MSTAE: "Materialstatus", MSTDE: "Status gültig ab", LAENG: "Länge",
    BREIT: "Breite", HOEHE: "Höhe", MEABM: "Maßeinheit", VOLUM: "Volumen", VOLEH: "Volumeneinheit",
    BSTME: "Bestellmengeneinheit", VABME: "Variable BME", LVORM: "Löschvormerkung", XCHPF: "Chargenpflicht",
    MFRNR: "Hersteller", MFRPN: "Herstellerteilenummer", HERKL: "Ursprungsland", ERGEW: "Zul. Verpackungsgewicht",
    ERGEI: "Gewichtseinheit Verp.", MHDRZ: "Mindestrestlaufzeit", MHDHB: "Gesamthaltbarkeit", ATTYP: "Materialkategorie",
    EKWSL: "Einkaufswerteschlüssel", PROFL: "Gefahrgutkennzeichen", KZUMW: "Umweltrelevant", MAGRV: "Materialgruppe PM",
    COLOR: "Farbe", SATNR: "Sammelartikel", PMATA: "Preismaterial", SERLV: "Serialnummernprofil"
  };
  const GENERAL = [
    ["Materialart", (m) => m.mara.MTART], ["Branche", (m) => m.mara.MBRSH], ["Warengruppe", (m) => m.mara.MATKL],
    ["Basismengeneinheit", (m) => m.mara.MEINS], ["EAN", (m) => (m.mara.EAN11 ? `${m.mara.EAN11} (${m.mara.NUMTP || "-"})` : "")],
    ["Alte Materialnummer", (m) => m.mara.BISMT],
    ["Brutto- / Nettogewicht", (m) => (m.mara.BRGEW ? `${m.mara.BRGEW} / ${m.mara.NTGEW || "-"} ${m.mara.GEWEI || ""}` : "")],
    ["L x B x H", (m) => (m.mara.LAENG ? `${m.mara.LAENG} x ${m.mara.BREIT} x ${m.mara.HOEHE} ${m.mara.MEABM || ""}` : "")],
    ["Volumen", (m) => (m.mara.VOLUM ? `${m.mara.VOLUM} ${m.mara.VOLEH || ""}` : "")],
    ["Sparte", (m) => m.mara.SPART], ["Produkthierarchie", (m) => m.mara.PRDHA], ["Transportgruppe", (m) => m.mara.TRAGR],
    ["Materialstatus", (m) => m.mara.MSTAE], ["Löschvormerkung", (m) => (m.mara.LVORM ? "Ja" : "")]
  ];

  return Controller.extend("fis.mdsa.controller.Material", {
    onInit() {
      this.getView().setModel(new JSONModel({ busy: true }), "m");
      this.getView().setModel(new JSONModel({ candidates: null, busy: false }), "d");
      this.getOwnerComponent().getRouter().getRoute("material").attachPatternMatched(this.onRouteMatched, this);
    },

    async onRouteMatched(event) {
      const matnr = event.getParameter("arguments").matnr;
      const m = this.getView().getModel("m");
      const d = this.getView().getModel("d");
      m.setData({ matnr, busy: true });
      d.setData({ candidates: null, busy: true, assessment: null });
      this.byId("page").setSelectedSection(null);
      this.matnr = matnr;
      try {
        const material = await api(`api/materials/${encodeURIComponent(matnr)}`);
        const maraFields = Object.entries(material.mara).map(([field, value]) => ({ field, value, label: MARA_LABELS[field] || "" }));
        m.setData({ ...material, maraFields, busy: false });
        this.buildGeneralForm(material);
      } catch (e) {
        m.setProperty("/busy", false);
        MessageBox.error(`Material ${matnr} konnte nicht geladen werden: ${e.message}`);
        return;
      }
      this.loadCandidates(matnr);
    },

    buildGeneralForm(material) {
      const form = this.byId("generalForm");
      form.destroyContent();
      GENERAL.forEach(([label, value]) => {
        form.addContent(new Label({ text: label }));
        form.addContent(new Text({ text: value(material) || "-" }));
      });
    },

    async loadCandidates(matnr) {
      const d = this.getView().getModel("d");
      try {
        const result = await api(`api/materials/${encodeURIComponent(matnr)}/duplicates`);
        if (matnr === this.matnr) {
          d.setData({ ...result, busy: false, assessment: null });
        }
      } catch (e) {
        d.setProperty("/busy", false);
        MessageToast.show(`Dublettenkandidaten konnten nicht ermittelt werden: ${e.message}`);
      }
    },

    async onAssess() {
      const d = this.getView().getModel("d");
      const matnr = this.matnr;
      const candidates = d.getProperty("/candidates").map((c) => c.matnr);
      d.setProperty("/busy", true);
      try {
        const assessment = await api(`api/materials/${encodeURIComponent(matnr)}/duplicates/assess`, { candidates });
        if (matnr !== this.matnr) {
          return;
        }
        const verdicts = Object.fromEntries(assessment.verdicts.map((v) => [v.matnr, v]));
        d.setProperty("/candidates", d.getProperty("/candidates").map((c) => ({ ...c, verdict: verdicts[c.matnr] || null })));
        d.setProperty("/assessment", assessment);
        d.setProperty("/duplicateFound", assessment.verdicts.some((v) => v.verdict === "Dublette" || v.verdict === "Mögliche Dublette"));
      } catch (e) {
        MessageBox.error(`KI-Bewertung fehlgeschlagen: ${e.message}`);
      } finally {
        d.setProperty("/busy", false);
      }
    },

    onScrollToDuplicates() {
      this.byId("page").scrollToSection(this.byId("duplicatesSection").getId());
    },

    onCandidatePress(event) {
      const matnr = event.getSource().getBindingContext("d").getProperty("matnr");
      this.getOwnerComponent().getRouter().navTo("material", { matnr });
    },

    onBack() {
      if (History.getInstance().getPreviousHash() !== undefined) {
        window.history.go(-1);
      } else {
        this.getOwnerComponent().getRouter().navTo("search", {}, true);
      }
    }
  });
});
