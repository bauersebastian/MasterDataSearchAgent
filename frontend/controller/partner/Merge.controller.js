sap.ui.define([
  "sap/ui/core/mvc/Controller",
  "sap/ui/core/routing/History",
  "sap/ui/core/Fragment",
  "sap/ui/core/util/File",
  "sap/ui/model/json/JSONModel",
  "sap/m/MessageBox",
  "sap/m/MessageToast",
  "sap/m/Dialog",
  "sap/m/Button",
  "sap/m/List",
  "sap/m/StandardListItem",
  "fis/mdsa/controller/api"
], (Controller, History, Fragment, File, JSONModel, MessageBox, MessageToast, Dialog, Button, List, StandardListItem, api) => {
  "use strict";

  const STEPS = ["stepKeep", "stepActions", "stepValues", "stepSend"];

  // Golden record wizard: one duplicate group -> keep one partner, take over the best data of the others, flag the
  // others for deletion / block them; the result goes to the DXTO staging table of the add-on and is logged
  return Controller.extend("fis.mdsa.controller.partner.Merge", {
    onInit() {
      this.model = new JSONModel({ partners: [] });
      this.getView().setModel(this.model, "m");
      this.getView().setModel(new JSONModel({}), "del");
      this.checks = {};    // partner -> deletion check result (kept while the wizard is open)
      this.getOwnerComponent().getRouter().getRoute("partnerMerge").attachPatternMatched(this.onRouteMatched, this);
    },

    async onRouteMatched(event) {
      const ids = event.getParameter("arguments").partners.split(",").filter(Boolean);
      const m = this.model;
      this.ids = ids;
      this.checks = {};
      this.touched = {};
      m.setData({ partners: [], others: [], busy: true, keep: "", advice: null, preview: null, result: null, log: [] });
      const wizard = this.byId("wizard");
      wizard.discardProgress(this.byId("stepKeep"));
      try {
        const proposal = await api("api/partners/merge/proposal", { partners: ids });
        if (ids !== this.ids) {
          return;
        }
        const log = proposal.log || [];
        m.setData({
          partners: proposal.partners.map((p) => ({ ...p, included: true })), keep: proposal.keep,
          suggested: proposal.keep, others: [], busy: false, advice: null, preview: null, result: null, tab: "changes",
          log,
          logText: log.length ? `Für diese Partner wurde bereits zusammengeführt: ${log.map((l) =>
            `Nr. ${l.id} am ${new Date(l.created_at).toLocaleString("de-DE")} (behalten ${l.keep}, `
            + `${l.status === "sent" ? "gesendet" : "fehlgeschlagen"}${l.current_extract ? "" : ", älterer Extrakt"})`).join("; ")}` : ""
        });
        this.updateKeep();
        this.assess();
      } catch (e) {
        m.setProperty("/busy", false);
        MessageBox.error(`Die Gruppe konnte nicht geladen werden: ${e.message}`);
      }
    },

    // the heuristic group of the overview contains possible duplicates only: the LLM judges every member against the
    // partner to keep; only real duplicates ("Dublette") stay ticked, "Mögliche Dublette" is the user's decision
    async assess() {
      const m = this.model;
      const reference = m.getProperty("/keep");
      const ids = this.ids;
      if (!this.getView().getModel().getProperty("/config/openai_configured")) {
        m.setProperty("/assessment", { error: "OPENAI_API_KEY ist nicht konfiguriert – keine KI-Bewertung, alle Partner der Gruppe sind einbezogen. Bitte selbst prüfen." });
        return;
      }
      const candidates = m.getProperty("/partners").map((p) => p.partner).filter((p) => p !== reference);
      m.setProperty("/assessing", true);
      m.setProperty("/assessment", null);
      this.updateKeep();
      try {
        const result = await api(`api/partners/${encodeURIComponent(reference)}/duplicates/assess`, { candidates });
        if (ids !== this.ids) {
          return;
        }
        const verdicts = Object.fromEntries(result.verdicts.map((v) => [v.partner, v]));
        m.setProperty("/partners", m.getProperty("/partners").map((p) => {
          if (p.partner === reference) {
            return { ...p, verdict: "Referenz", verdict_reason: "", included: true };
          }
          const v = verdicts[p.partner];
          return { ...p, verdict: v ? v.verdict : "nicht bewertet", verdict_reason: v ? v.reason : "",
            included: !!v && v.verdict === "Dublette" };
        }));
        const duplicates = result.verdicts.filter((v) => v.verdict === "Dublette").length;
        m.setProperty("/assessment", { reference, summary: result.summary, duplicates });
      } catch (e) {
        m.setProperty("/assessment", { error: `KI-Bewertung fehlgeschlagen: ${e.message} – bitte die Gruppe selbst prüfen.` });
      } finally {
        if (ids === this.ids) {
          m.setProperty("/assessing", false);
          this.updateKeep();
        }
      }
    },

    onReassess() {
      this.assess();
    },

    included() {
      return this.model.getProperty("/partners").filter((p) => p.included);
    },

    // --- step 1: partner to keep ---
    onKeepSelect(event) {
      if (event.getParameter("selected")) {
        this.model.setProperty("/keep", event.getSource().getBindingContext("m").getProperty("partner"));
        this.updateKeep();
      }
    },

    onIncludeChange() {
      this.updateKeep();
    },

    // hints for the chosen partner and reset of the later steps
    updateKeep() {
      const m = this.model;
      const keepId = m.getProperty("/keep");
      const partners = this.included();
      const keep = partners.find((p) => p.partner === keepId);
      const others = partners.filter((p) => p.partner !== keepId);
      const hints = [];
      if (keep) {
        const missing = (attr) => [...new Set(others.flatMap((p) => p[attr]))].filter((x) => !keep[attr].includes(x));
        const areas = missing("sales_areas");
        const orgs = missing("purchasing_orgs");
        if (areas.length) {
          hints.push(`Vertriebsbereiche der anderen fehlen beim behaltenen Partner: ${areas.join(", ")} – bei Bedarf im Add-on anlegen.`);
        }
        if (orgs.length) {
          hints.push(`Einkaufsorganisationen der anderen fehlen beim behaltenen Partner: ${orgs.join(", ")} – bei Bedarf im Add-on anlegen.`);
        }
        if (!keep.has_customer && others.some((p) => p.has_customer)) {
          hints.push("Der behaltene Partner ist kein Kunde: die Kundenrolle wird von einer Dublette übernommen (neue Kundennummer vom Add-on).");
        }
        if (!keep.has_vendor && others.some((p) => p.has_vendor)) {
          hints.push("Der behaltene Partner ist kein Lieferant: die Lieferantenrolle wird von einer Dublette übernommen (neue Lieferantennummer vom Add-on).");
        }
        if (keep.deleted || keep.blocks.length) {
          hints.push(`Der behaltene Partner ${keep.partner} ist gelöscht oder gesperrt.`);
        }
      }
      m.setProperty("/hints", hints);
      m.setProperty("/keepValid", !!keep && others.length > 0 && !m.getProperty("/assessing"));
      m.setProperty("/advice", null);
      m.setProperty("/preview", null);
      m.setProperty("/result", null);
      this.discardAfter("stepKeep");
    },

    // back to `step` if the wizard is already further (later steps depend on it)
    discardAfter(stepId) {
      const wizard = this.byId("wizard");
      if (wizard.getProgress() > STEPS.indexOf(stepId) + 1) {
        wizard.discardProgress(this.byId(stepId));
      }
    },

    onPartnerPress(event) {
      const partner = event.getSource().getBindingContext("m").getProperty("partner");
      this.getOwnerComponent().getRouter().navTo("partner", { partner });
    },

    // --- step 2: deletion flag or blocks ---
    onActionsActivate() {
      const m = this.model;
      const keep = m.getProperty("/keep");
      const previous = Object.fromEntries((m.getProperty("/others") || []).map((o) => [o.partner, o]));
      const others = this.included().filter((p) => p.partner !== keep).map((p) => previous[p.partner] || {
        partner: p.partner, name: p.name, kunnr: p.kunnr, lifnr: p.lifnr, has_customer: p.has_customer,
        has_vendor: p.has_vendor, check: this.checks[p.partner] || null, checking: false,
        delete: false, central_block: false, customer_block: false, vendor_block: false
      });
      m.setProperty("/others", others);
      others.forEach((o, i) => {
        if (o.check) {
          this.applyDefaults(i);
        } else {
          this.runCheck(o.partner, false);
        }
      });
      this.validateActions();
    },

    async runCheck(partner, refresh) {
      const m = this.model;
      const index = () => m.getProperty("/others").findIndex((o) => o.partner === partner);
      m.setProperty(`/others/${index()}/checking`, true);
      this.validateActions();
      let check;
      try {
        check = await api(`api/partners/merge/deletion-check/${encodeURIComponent(partner)}?refresh=${!!refresh}`, {});
      } catch (e) {
        check = { verdict: "incomplete", text: `Löschprüfung fehlgeschlagen: ${e.message}`, checks: null };
      }
      this.checks[partner] = check;
      const i = index();
      if (i < 0) {
        return;
      }
      m.setProperty(`/others/${i}/check`, check);
      m.setProperty(`/others/${i}/checking`, false);
      this.applyDefaults(i);
      this.validateActions();
    },

    // without open documents: deletion flag; otherwise all applicable blocks (until the user changes it)
    applyDefaults(i) {
      const m = this.model;
      const o = m.getProperty(`/others/${i}`);
      const ok = o.check && o.check.verdict === "ok";
      if (!ok && o.delete) {
        m.setProperty(`/others/${i}/delete`, false);
      }
      if (this.touched[o.partner]) {
        return;
      }
      m.setProperty(`/others/${i}`, { ...m.getProperty(`/others/${i}`), delete: ok, central_block: !ok,
        customer_block: !ok && o.has_customer, vendor_block: !ok && o.has_vendor });
    },

    onRecheck(event) {
      const partner = event.getSource().getBindingContext("m").getProperty("partner");
      this.runCheck(partner, true);
      this.discardAfter("stepActions");
    },

    onActionChange(event) {
      this.touched[event.getSource().getBindingContext("m").getProperty("partner")] = true;
      this.model.setProperty("/preview", null);
      this.model.setProperty("/result", null);
      this.validateActions();
      this.discardAfter("stepActions");
    },

    validateActions() {
      const others = this.model.getProperty("/others") || [];
      this.model.setProperty("/actionsValid", others.length > 0 && others.every((o) => !o.checking
        && (o.delete || o.central_block || o.customer_block || o.vendor_block)));
    },

    // deletion check details in the dialog of the partner page
    async onShowCheck(event) {
      const o = event.getSource().getBindingContext("m").getObject();
      if (!this.deletionDialog) {
        this.deletionDialog = await Fragment.load({ id: this.getView().getId(), name: "fis.mdsa.view.partner.DeletionCheck", controller: this });
        this.getView().addDependent(this.deletionDialog);
      }
      this.checkPartner = o.partner;
      this.showCheck(o.check);
      this.deletionDialog.open();
    },

    showCheck(check) {
      this.getView().getModel("del").setData({ ...check, partner: this.checkPartner, busy: false,
        checked_text: check.checked_at ? new Date(check.checked_at).toLocaleString("de-DE") : "" });
    },

    async onRunDeletionCheck() {
      this.getView().getModel("del").setProperty("/busy", true);
      await this.runCheck(this.checkPartner, true);
      this.discardAfter("stepActions");
      this.showCheck(this.checks[this.checkPartner]);
    },

    onCloseDeletionCheck() {
      this.deletionDialog.close();
    },

    formatDate(iso) {
      return iso ? new Date(`${iso}T00:00:00`).toLocaleDateString("de-DE") : "";
    },

    formatAmount(value) {
      return value === undefined || value === null ? "" : Number(value).toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    },

    // --- step 3: best values (LLM) ---
    async onValuesActivate() {
      const m = this.model;
      if (m.getProperty("/advice")) {
        return;
      }
      const body = { partners: this.included().map((p) => p.partner), keep: m.getProperty("/keep") };
      const key = JSON.stringify(body);
      this.adviceKey = key;
      m.setProperty("/adviceBusy", true);
      try {
        const advice = await api("api/partners/merge/advice", body);
        if (key === this.adviceKey) {
          m.setProperty("/advice", advice);
        }
      } catch (e) {
        MessageBox.error(`Die Werte konnten nicht ermittelt werden: ${e.message}`);
      } finally {
        if (key === this.adviceKey) {
          m.setProperty("/adviceBusy", false);
        }
      }
    },

    onValueChange() {
      this.model.setProperty("/preview", null);
      this.model.setProperty("/result", null);
      this.discardAfter("stepValues");
    },

    // --- step 4: review and send ---
    request() {
      const m = this.model;
      const advice = m.getProperty("/advice") || { groups: [], items: [] };
      return {
        partners: this.included().map((p) => p.partner),
        keep: m.getProperty("/keep"),
        choices: Object.fromEntries(advice.groups.map((g) => [g.key, g.choice])),
        take: advice.items.filter((i) => i.take).map((i) => i.key),
        actions: Object.fromEntries(m.getProperty("/others").map((o) => [o.partner,
          ["delete", "central_block", "customer_block", "vendor_block"].filter((a) => o[a])]))
      };
    },

    async onSendActivate() {
      const m = this.model;
      m.setProperty("/previewBusy", true);
      m.setProperty("/result", null);
      m.setProperty("/tab", "changes");
      try {
        m.setProperty("/preview", await api("api/partners/merge/preview", this.request()));
      } catch (e) {
        MessageBox.error(`Die Nachricht konnte nicht erzeugt werden: ${e.message}`);
      } finally {
        m.setProperty("/previewBusy", false);
      }
    },

    formatFields(fields) {
      if (!Array.isArray(fields)) {
        return "";
      }
      const shown = fields.slice(0, 10).map((f) => `${f.field}: ${f.old ? f.old + " → " : ""}${f.new || "(leer)"}`);
      return shown.join("; ") + (fields.length > 10 ? ` … (+${fields.length - 10})` : "");
    },

    onDownload() {
      const preview = this.model.getProperty("/preview");
      File.save(preview.xml, preview.file.replace(/\.xml$/, ""), "xml", "application/xml", "utf-8");
    },

    onSend() {
      const m = this.model;
      const preview = m.getProperty("/preview");
      MessageBox.confirm(`Golden Record für ${preview.keep} (${preview.changes.length} Änderungen an `
        + `${preview.others.length + 1} Partnern) an FIS/mpm senden (Staging-Tabelle DXTO)?\n\n${preview.endpoint}`, {
        title: "An FIS/mpm senden",
        emphasizedAction: MessageBox.Action.OK,
        onClose: async (action) => {
          if (action !== MessageBox.Action.OK) {
            return;
          }
          m.setProperty("/busy", true);
          try {
            const sent = await api("api/partners/merge/send", this.request());
            m.setProperty("/preview", sent);
            m.setProperty("/result", sent.result);
            m.setProperty("/logId", sent.log_id);
            if (sent.result.ok) {
              MessageToast.show("Golden Record an FIS/mpm gesendet und protokolliert");
            } else {
              m.setProperty("/tab", "response");
              MessageBox.error(`SAP hat die Nachricht nicht angenommen (HTTP ${sent.result.status_code}). Details im Reiter SAP-Antwort.`);
            }
          } catch (e) {
            MessageBox.error(`Senden nicht möglich: ${e.message}`);
          } finally {
            m.setProperty("/busy", false);
          }
        }
      });
    },

    // --- log of all merges ---
    async onShowLog() {
      let entries;
      try {
        entries = await api("api/partners/merge/log");
      } catch (e) {
        MessageBox.error(`Protokoll konnte nicht geladen werden: ${e.message}`);
        return;
      }
      const list = new List({ noDataText: "Noch keine Zusammenführung gesendet" });
      for (const e of entries) {
        const actions = Object.entries(e.actions).map(([p, a]) => `${p}: ${a.join("/")}`).join(", ");
        list.addItem(new StandardListItem({
          title: `Nr. ${e.id} · behalten ${e.keep} · ${e.partners.filter((p) => p !== e.keep).join(", ")}`,
          description: `${new Date(e.created_at).toLocaleString("de-DE")} · Extrakt ${e.extract}${e.current_extract ? "" : " (älter)"} · ${actions}`,
          info: e.status === "sent" ? "gesendet" : `fehlgeschlagen ${e.http_status || ""}`,
          infoState: e.status === "sent" ? "Success" : "Error"
        }));
      }
      const dialog = new Dialog({
        title: "Protokoll Golden Record", contentWidth: "48rem", contentHeight: "60%", resizable: true,
        content: [list], endButton: new Button({ text: "Schließen", press: () => dialog.close() }),
        afterClose: () => dialog.destroy()
      });
      this.getView().addDependent(dialog);
      dialog.open();
    },

    onHelp() {
      this.getOwnerComponent().getRouter().navTo("help");
    },

    onBack() {
      if (History.getInstance().getPreviousHash() !== undefined) {
        window.history.go(-1);
      } else {
        this.getOwnerComponent().getRouter().navTo("partnerDuplicates", {}, true);
      }
    }
  });
});
