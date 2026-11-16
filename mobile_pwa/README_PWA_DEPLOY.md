# Deploy FlowLocal Mobile/Web PWA (GitHub Pages HTTPS)

La cartella `mobile_pwa/` contiene la **PWA autonoma di FlowLocal** (elaborazione in-loco con Gemini API BYOK, Google Drive `drive.file`, Google Picker API, IndexedDB offline, RSVP Speed-Reading e Modalità Aula con Screen Wake Lock).

## 1. Deploy Automatico su GitHub Pages
Il repository include già il workflow [deploy-pwa.yml](file:///c:/Users/aless/Projects/flow-local/.github/workflows/deploy-pwa.yml).

1. Vai su **GitHub → Repository → Settings → Pages**.
2. Alla voce **Build and deployment → Source**, seleziona **GitHub Actions**.
3. Ogni push su `main` che modifica `mobile_pwa/**` pubblicherà automaticamente la PWA all'indirizzo HTTPS:
   `https://<tuo-username>.github.io/<nome-repo>/`

## 2. Configurazione Google Cloud Console (OAuth2 + Google Picker)
Affinché il login Google Drive (`drive.file`) e Google Picker funzionino sulla PWA in HTTPS:

1. Apri **Google Cloud Console → APIs & Services → Enabled APIs** e abilita:
   - **Google Drive API**
   - **Google Picker API**
2. Vai in **Credentials → Create Credentials → OAuth client ID** (tipo *Web application*).
3. In **Authorized JavaScript origins** aggiungi l'URL HTTPS della tua GitHub Page (es. `https://<tuo-username>.github.io`).
4. Copia il **Client ID** (e opzionalmente una **API Key** limitata al dominio per Google Picker) e incollali nella scheda **Impostazioni** della PWA.

## 3. Chiave Gemini API Gratuita (Elaborazione In-Loco)
1. Apri [Google AI Studio](https://aistudio.google.com/apikey) e crea una chiave gratuita.
2. Incollala nella scheda **Impostazioni → Gemini API Key** della PWA per abilitare sbobine audio, dettatura multimodale, OCR da fotocamera e Whis direttamente da smartphone/tablet.
