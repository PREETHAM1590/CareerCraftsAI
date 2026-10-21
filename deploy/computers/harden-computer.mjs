import { readFile, writeFile } from 'node:fs/promises';

// Fail closed against the pinned source. There is no new evaluate/exec surface.
const path = process.argv[2];
let source = await readFile(path, 'utf8');
const anchor = '      if (actsOnTheComputer(url.pathname)) {';
if (source.split(anchor).length !== 2) throw new Error('Review changed OpenBot request contract');
source = 'import { access, readFile } from "node:fs/promises";\nconst privateMarker = "/profiles/.careercraft-private-input";\nfunction privatePageKey(value:string) { const u=new URL(value); return u.origin+u.pathname; }\nasync function privateInput() { return access(privateMarker).then(()=>true,()=>false); }\n' + source;
source = source.replace(anchor, `
      if (url.pathname === "/upload" && request.method === "POST") {
        let release: (()=>void) | undefined;
        try {
          const target = await currentPage(botId);
          const actor = request.headers.get("x-careercraft-actor");
          if (!["human","agent"].includes(actor || "")) return json({error:"Invalid actor"},403);
          if (actor === "human" && !session.control.humanMayDrive()) return json({error:"Take control first"},409);
          if (actor === "agent" && (await privateInput() ||
              await target.locator('input[type="password"]').evaluateAll(nodes=>nodes.some(n=>(n as HTMLInputElement).value.length>0)))) return json({error:"Human or private control is active"},409);
          if (actor === "agent") release = session.control.admitBotAction(true);
          if (Number(request.headers.get("x-careercraft-snapshot")) !== session.snapshotId) return json({error:"Reviewed snapshot changed"},409);
          const body = await request.json() as {ref:string;snapshotId:number;filename:string;mimeType:string;base64:string};
          if (body.snapshotId !== session.snapshotId || typeof body.base64 !== "string" || body.base64.length > 14000000 ||
              !["application/pdf","application/vnd.openxmlformats-officedocument.wordprocessingml.document","text/plain"].includes(body.mimeType) ||
              typeof body.filename !== "string" || /[\\\\/]/.test(body.filename)) return json({error:"Invalid resume upload"},422);
          const input = locateRef(session,target,body.ref,body.snapshotId);
          if (await input.getAttribute("type") !== "file") return json({error:"Choose a file input"},422);
          await input.setInputFiles({name:body.filename,mimeType:body.mimeType,buffer:Buffer.from(body.base64,"base64")},{timeout:ACTION_TIMEOUT_MS});
          session.snapshotId += 1;
          return json({uploaded:true,submitted:false});
        } catch { return json({error:"Resume upload failed; inspect the page before retrying"},502); }
        finally { release?.(); }
      }
      if (url.pathname === "/human/navigate" && request.method === "POST") {
        if (request.headers.get("x-careercraft-actor") !== "human" || !session.control.humanMayDrive()) return json({error:"Take control first"},409);
        const body = await request.json() as {url:unknown};
        const parsed = parseNavigateUrl(body.url);
        if (!parsed.ok) return json({error:"Invalid web URL"},422);
        await navigateWebPage(await currentPage(botId,"navigate"),parsed.url,RUNTIME.backend,NAVIGATION_TIMEOUT_MS);
        session.snapshotId += 1;
        return json({navigated:true});
      }
      if (url.pathname === "/credentials/fill" && request.method === "POST") {
        if (request.headers.get("x-careercraft-actor") !== "human") return json({error:"Human-only credential path"},403);
        try {
        const body = await request.json() as {origin:string;username:string;password:string;usernameRef:string;passwordRef:string;snapshotId:number};
        const target = await currentPage(botId);
        if (new URL(target.url()).origin !== body.origin) return json({error:"Credential origin mismatch"},403);
        const userField = locateRef(session, target, body.usernameRef, body.snapshotId);
        const passField = locateRef(session, target, body.passwordRef, body.snapshotId);
        if (await userField.evaluate(el=>el.ownerDocument.location.origin) !== body.origin ||
            await passField.evaluate(el=>el.ownerDocument.location.origin) !== body.origin) return json({error:"Credential field origin mismatch"},403);
        if (await passField.getAttribute("type") !== "password") return json({error:"A password input is required"},422);
        const userType = await userField.getAttribute("type");
        if (![null,"text","email"].includes(userType)) return json({error:"Use a username or email input"},422);
        await writeFile(privateMarker,privatePageKey(target.url()));
        await userField.fill(body.username,{timeout:ACTION_TIMEOUT_MS});
        await passField.fill(body.password,{timeout:ACTION_TIMEOUT_MS});
        return json({filled:true,submitted:false});
        } catch {
          // Playwright failures can include typed values in their call logs.
          // Keep them out of responses and Bun's uncaught-error logger.
          return json({error:"Private credential entry failed"},502);
        }
      }
      if (url.pathname === "/privacy/release" && request.method === "POST") {
        if (request.headers.get("x-careercraft-actor") !== "human") return json({error:"Human-only privacy path"},403);
        const target = await currentPage(botId);
        const privatePage = await readFile(privateMarker,"utf8").catch(()=>null);
        if (privatePage === "private" || privatePage === privatePageKey(target.url()) ||
            await target.locator('input[type="password"]').count()) return json({error:"Leave the private input page before returning control"},409);
        await rm(privateMarker,{force:true});
        return json({released:true});
      }
      if (url.pathname === "/human/secret" || url.pathname === "/human/type" || url.pathname === "/human/key") {
        await writeFile(privateMarker,privatePageKey((await currentPage(botId)).url()));
      }
      if (request.headers.get("x-careercraft-actor") === "agent" &&
          ["/navigate","/read","/snapshot","/click","/type","/scroll"].includes(url.pathname)) {
        const target = await currentPage(botId);
        const filledPassword = await target.locator('input[type="password"]').evaluateAll(nodes => nodes.some(n => (n as HTMLInputElement).value.length > 0));
        if (filledPassword || await privateInput()) return json({error:"Private login or human input is in progress"},409);
      }
      if (request.headers.get("x-careercraft-actor") === "agent" &&
          ["/navigate","/click","/type","/scroll"].includes(url.pathname) &&
          Number(request.headers.get("x-careercraft-snapshot")) !== session.snapshotId) {
        return json({error:"Reviewed snapshot changed"},409);
      }
${anchor}`);
await writeFile(path, source);

// Force all browser traffic through the private-address-denying proxy. The
// computer network is internal, so a browser cannot bypass this to the internet.
const profilesPath = path.replace(/index\.ts$/, 'profiles.ts');
let profiles = await readFile(profilesPath, 'utf8');
const proxyAnchor = 'const proxy = egressFor(botId, process.env);';
if (profiles.split(proxyAnchor).length !== 2) throw new Error('Review changed browser egress contract');
// By address, not the name "egress": Docker's embedded DNS (127.0.0.11) does not
// answer inside gVisor (runsc) containers. compose.yml pins egress to this IP.
profiles = profiles.replace(proxyAnchor, 'const proxy = {server:"http://172.31.250.10:3128",bypass:"<-loopback>"};');
await writeFile(profilesPath, profiles);

// Screen refreshes poll a screenshot every 4 s per open panel. PNG encoding of a
// 1280x800 page dominated CPU under load and made each frame large; JPEG q70
// (what the screencast already uses) is far cheaper and smaller. `mime` lets
// the app render either format.
const index = await readFile(path, 'utf8');
const shotAnchor = 'const buffer = await target.screenshot({ type: "png" });';
const shotBody = 'base64: buffer.toString("base64"),';
if (index.split(shotAnchor).length !== 2 || index.split(shotBody).length !== 2) {
  throw new Error('Review changed screenshot contract');
}
await writeFile(
  path,
  index
    .replace(shotAnchor, 'const buffer = await target.screenshot({ type: "jpeg", quality: 70 });')
    .replace(shotBody, `${shotBody} mime: "image/jpeg",`),
);

// Background work every Chromium does for a desktop user (translate, media
// routing, optimisation hints, sync) costs CPU in every concurrent computer and
// serves no job application. Safe Browsing and CRLSet updates stay on.
let launchProfiles = await readFile(profilesPath, 'utf8');
const flagsAnchor = '  "--disable-dev-shm-usage",';
if (launchProfiles.split(flagsAnchor).length !== 2) throw new Error('Review changed launch flags');
launchProfiles = launchProfiles.replace(flagsAnchor, `${flagsAnchor}
  "--disable-default-apps",
  "--disable-sync",
  "--no-first-run",
  "--metrics-recording-only",
  "--disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication,InterestFeedContentSuggestions",`);
await writeFile(profilesPath, launchProfiles);
