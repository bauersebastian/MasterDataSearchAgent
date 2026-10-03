sap.ui.define([
  "sap/ui/core/mvc/Controller",
  "sap/ui/core/routing/History",
  "sap/ui/model/json/JSONModel",
  "sap/m/MessageBox",
  "sap/m/MessageToast",
  "sap/m/Label",
  "sap/m/Text",
  "sap/ui/core/Fragment",
  "sap/ui/core/util/File",
  "fis/mdsa/controller/api"
], (Controller, History, JSONModel, MessageBox, MessageToast, Label, Text, Fragment, File, api) => {
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

  // visible rows of a fully expanded tree table: all nodes, between 1 and 15
  const countNodes = (nodes) => nodes.reduce((n, node) => n + 1 + countNodes(node.children || []), 0);
  const rowCount = (nodes) => Math.min(Math.max(countNodes(nodes), 1), 15);

  return Controller.extend("fis.mdsa.controller.Material", {
    onInit() {
      this.getView().setModel(new JSONModel({ busy: true }), "m");
      this.getView().setModel(new JSONModel({ candidates: null, busy: false }), "d");
      this.getView().setModel(new JSONModel({}), "s");
      this.getView().setModel(new JSONModel({}), "b");
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
      this.getView().getModel("b").setData({ stlan: "", werks: "" });
      this.loadBom(matnr);
      this.loadCandidates(matnr);
    },

    // --- bill of material relations (graph, multi-level explosion and where-used list) ---
    async loadBom(matnr) {
      const b = this.getView().getModel("b");
      const params = new URLSearchParams({ stlan: b.getProperty("/stlan") || "", werks: b.getProperty("/werks") || "" });
      try {
        const result = await api(`api/materials/${encodeURIComponent(matnr)}/bom?${params}`);
        if (matnr !== this.matnr) {
          return;
        }
        b.setData({
          ...result, stlan: b.getProperty("/stlan"), werks: b.getProperty("/werks"),
          usageItems: [{ key: "", text: "Alle Verwendungen" }, ...result.options.stlan],
          plantItems: [{ key: "", text: "Alle Werke" }, ...result.options.werks],
          explosionRows: rowCount(result.explosion), whereUsedRows: rowCount(result.where_used)
        });
        // show the whole structure expanded (the extract has at most 4 levels)
        ["explosionTable", "where_usedTable"].forEach((id) => this.byId(id).expandToLevel(10));
      } catch (e) {
        MessageToast.show(`Stücklisten konnten nicht geladen werden: ${e.message}`);
      }
    },

    // large graphs: zoom out so that all nodes are visible (the toolbar's "zoom to fit" - no public API for it)
    onGraphReady(event) {
      const graph = event.getSource();
      if (typeof graph._fitToScreen === "function" && graph.$scroller
        && (graph._iWidth > graph.$scroller.width() || graph._iHeight > graph.$scroller.height())) {
        graph._fitToScreen();
      }
    },

    onBomFilterChange() {
      this.loadBom(this.matnr);
    },

    onBomMaterialPress(event) {
      const matnr = event.getSource().getBindingContext("b").getProperty("matnr");
      if (matnr && matnr !== this.matnr) {
        this.getOwnerComponent().getRouter().navTo("material", { matnr });
      }
    },

    onGraphNodeOpen(event) {
      const matnr = event.getSource().getParent().getKey();
      if (matnr !== this.matnr) {
        this.getOwnerComponent().getRouter().navTo("material", { matnr });
      }
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

    // --- send the material to SAP (DXTO import message, unchanged extract data) ---
    async onOpenSapDialog() {
      const s = this.getView().getModel("s");
      s.setData({ matnr: this.matnr, busy: true, tab: "xml", postResult: null });
      if (!this.sapDialog) {
        this.sapDialog = await Fragment.load({ id: this.getView().getId(), name: "fis.mdsa.view.SapDialog", controller: this });
        this.getView().addDependent(this.sapDialog);
      }
      this.sapDialog.open();
      try {
        const message = await api(`api/materials/${encodeURIComponent(this.matnr)}/xml`);
        s.setData({ ...message, busy: false, tab: "xml", postResult: null });
      } catch (e) {
        s.setProperty("/busy", false);
        MessageBox.error(`XML konnte nicht erzeugt werden: ${e.message}`);
      }
    },

    onCloseSapDialog() {
      this.sapDialog.close();
    },

    onSapDialogClosed() {
      this.getView().getModel("s").setData({});
    },

    onCopyXml() {
      navigator.clipboard.writeText(this.getView().getModel("s").getProperty("/xml"))
        .then(() => MessageToast.show("XML in die Zwischenablage kopiert"))
        .catch((e) => MessageBox.error(e.message));
    },

    onDownloadXml() {
      const s = this.getView().getModel("s");
      File.save(s.getProperty("/xml"), `material_${s.getProperty("/matnr")}`, "xml", "application/xml", "utf-8");
    },

    onPostToSap() {
      const s = this.getView().getModel("s");
      const matnr = s.getProperty("/matnr");
      const warning = s.getProperty("/deleted") ? "\n\nAchtung: Das Material hat eine Löschvormerkung." : "";
      MessageBox.confirm(`Material ${matnr} an SAP senden?\n\n${s.getProperty("/endpoint")}${warning}`, {
        title: "An SAP senden",
        emphasizedAction: MessageBox.Action.OK,
        onClose: async (action) => {
          if (action !== MessageBox.Action.OK) {
            return;
          }
          s.setProperty("/busy", true);
          try {
            const result = await api("api/sap/post", { xml: s.getProperty("/xml") });
            s.setProperty("/postResult", result);
            s.setProperty("/tab", "response");
            if (result.ok) {
              MessageToast.show(`SAP hat die Nachricht angenommen (HTTP ${result.status_code})`);
            } else {
              MessageBox.error(`SAP-Aufruf fehlgeschlagen (HTTP ${result.status_code}${result.fault ? ", SOAP-Fault" : ""}). Details im Reiter SAP-Antwort.`);
            }
          } catch (e) {
            MessageBox.error(`Senden fehlgeschlagen: ${e.message}`);
          } finally {
            s.setProperty("/busy", false);
          }
        }
      });
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
