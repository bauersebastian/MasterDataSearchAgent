sap.ui.define([
  "sap/ui/core/Fragment",
  "sap/ui/core/util/File",
  "sap/ui/model/json/JSONModel",
  "sap/m/MessageBox",
  "sap/m/MessageToast",
  "fis/mdsa/controller/api"
], (Fragment, File, JSONModel, MessageBox, MessageToast, api) => {
  "use strict";

  // "An SAP senden" dialog (DXTO import message, unchanged extract data) for materials and business partners;
  // mixed into the object page controllers: Controller.extend(name, { ...SapDialog, ... })
  return {
    // what: { path: "api/.../xml", label: "Material 4711", subject: "Das Material", file: "material_4711" }
    async openSapDialog(what) {
      const view = this.getView();
      if (!view.getModel("s")) {
        view.setModel(new JSONModel({}), "s");
      }
      const s = view.getModel("s");
      const base = { ...what, busy: false, tab: "xml", postResult: null };
      s.setData({ ...base, busy: true });
      if (!this.sapDialog) {
        this.sapDialog = await Fragment.load({ id: view.getId(), name: "fis.mdsa.view.SapDialog", controller: this });
        view.addDependent(this.sapDialog);
      }
      this.sapDialog.open();
      try {
        const message = await api(what.path);
        s.setData({ ...message, ...base });
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
      File.save(s.getProperty("/xml"), s.getProperty("/file"), "xml", "application/xml", "utf-8");
    },

    onPostToSap() {
      const s = this.getView().getModel("s");
      const warning = s.getProperty("/deleted") ? `\n\nAchtung: ${s.getProperty("/subject")} hat eine Löschvormerkung.` : "";
      MessageBox.confirm(`${s.getProperty("/label")} an SAP senden?\n\n${s.getProperty("/endpoint")}${warning}`, {
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
    }
  };
});
