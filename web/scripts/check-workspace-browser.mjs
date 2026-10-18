/**
 * Exercise a running workspace through two newly registered QA accounts.
 * This deliberately creates accounts, a test project and versioned experiments,
 * then exercises edits, historical copies, conflicts and deletions.
 * Run only against a deployment you are authorized to test. Created usernames
 * are recorded for administrator cleanup; passwords and cookies are not saved.
 * Requires an explicit workspace URL, output directory and installed Playwright.
 */
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const [baseArgument, outputArgument, ...extraArguments] = process.argv.slice(2);
if (!baseArgument || !outputArgument || extraArguments.length) {
  throw new Error('Usage: node web/scripts/check-workspace-browser.mjs WORKSPACE_URL OUTPUT_DIRECTORY');
}
const targetUrl = new URL(baseArgument);
if (!['http:', 'https:'].includes(targetUrl.protocol) || targetUrl.username || targetUrl.password || targetUrl.search || targetUrl.hash) {
  throw new Error('Use an HTTP(S) workspace URL without credentials, query parameters or a fragment.');
}
const base = targetUrl.href;
const output = resolve(outputArgument) + sep;
const playwrightModule = process.env.MERIDIAN_PLAYWRIGHT_MODULE
  ? pathToFileURL(resolve(process.env.MERIDIAN_PLAYWRIGHT_MODULE)).href
  : new URL('../../build/browser-tools/node_modules/playwright/index.mjs', import.meta.url).href;
const { chromium } = await import(playwrightModule);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  ...(process.env.MERIDIAN_BROWSER_EXECUTABLE ? { executablePath: process.env.MERIDIAN_BROWSER_EXECUTABLE } : {}),
});
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const other = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
const peer = await other.newPage();
const pageErrors = [];
const cspErrors = [];
const consoleErrors = [];
for (const target of [page, peer]) target.on('console', message => {
  if (message.type() !== 'error') return;
  consoleErrors.push(message.text());
  if (/content security policy|violat.*(?:directive|policy)|refused to (?:load|execute|apply|connect)|blocked.*(?:script|style|font)/i.test(message.text())) cspErrors.push(message.text());
});
for (const target of [page, peer]) target.on('pageerror', error => pageErrors.push(error.message));
const checks = [];
const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };
const stamp = `${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`;
const createdAccounts = [];
let contentSecurityPolicy = null;
let executionEnabled = false;
let failure = null;
const username = `qa-owner-${stamp}`;
const peerUsername = `qa-peer-${stamp}`;
const password = `Qa!${randomBytes(24).toString('base64url')}`;
async function register(target, user) {
  await target.goto(base);
  await target.getByRole('button', { name: 'Create account', exact: true }).click();
  await target.getByLabel('Username', { exact: true }).fill(user);
  await target.getByLabel('Password', { exact: true }).fill(password);
  const [registrationResponse] = await Promise.all([
    target.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/auth/register' && response.request().method() === 'POST'),
    target.getByRole('button', { name: 'Create account', exact: true }).click(),
  ]);
  if (registrationResponse.ok()) createdAccounts.push(user);
  await target.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
}
async function noOverflow(target, label) {
  check(label, await target.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
}
async function apiRequest(target, path, method = 'GET', body) {
  return target.evaluate(async ({ path, method, body }) => {
    const session = await (await fetch('/api/v1/session')).json();
    const response = await fetch(`/api/v1${path}`, {
      method, headers: { 'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: response.status, payload: await response.json() };
  }, { path, method, body });
}
try {
  const firstResponse = await page.goto(base);
  contentSecurityPolicy = await firstResponse.headerValue('content-security-policy');
  check('deployment serves a Content-Security-Policy header', Boolean(contentSecurityPolicy));
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor();
  await page.screenshot({ path: `${output}signin-dark.png`, fullPage: true });
  await register(page, username);
  executionEnabled = Boolean((await apiRequest(page, '/session')).payload.capabilities.execution);
  check('registration creates an authenticated empty private workspace', await page.getByRole('heading', { name: 'No projects yet' }).isVisible());
  await page.getByRole('button', { name: /New project/ }).click();
  await page.getByLabel('Project name', { exact: true }).fill('Indoor VIO reference');
  await page.getByLabel('Description', { exact: false }).fill('Stereo + IMU integration study. <img src=x onerror="window.injected=true">');
  await page.getByRole('button', { name: 'Create project', exact: true }).click();
  await page.getByRole('heading', { name: 'TUM-VI room1', exact: true }).waitFor();
  const projectId = new URL(page.url()).hash.split('/')[1];
  check('created project uses a persistent UUID route', /^[a-f\d-]{36}$/.test(projectId));
  check('project description renders as text rather than HTML', !(await page.evaluate(() => window.injected)) && await page.locator('#app img').count() === 0);
  check('catalog reference coverage is shown independently', await page.getByText('97.70%', { exact: true }).isVisible());
  check('no disconnected execution controls exist', await page.getByRole('button', { name: /run|launch|execute/i }).count() === 0);
  await page.reload();
  await page.getByRole('heading', { name: 'Indoor VIO reference', exact: true }).waitFor();
  check('project persists across refresh and deep links', true);
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByLabel('Project name', { exact: true }).fill('VIO · Stereo reference');
  await page.getByLabel('Description', { exact: true }).fill('Reference recording for indoor visual-inertial estimation.');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await page.getByRole('heading', { name: 'VIO · Stereo reference', exact: true }).waitFor();
  check('project name and description save through backend', true);
  await page.getByRole('link', { name: 'Data', exact: true }).click();
  await page.getByRole('heading', { name: 'TUM-VI room1', exact: true }).waitFor();
  await page.screenshot({ path: `${output}dataset-dark.png`, fullPage: true });
  await page.getByRole('link', { name: 'Experiments', exact: true }).click();
  await page.getByRole('heading', { name: 'No experiments in this project' }).waitFor();
  check('new projects have an empty experiment collection', true);
  const profile = (await apiRequest(page, '/configuration-profile')).payload.profile;
  const parameterLabel = profile.parameters.num_pts.label;
  await page.getByRole('button', { name: 'New experiment', exact: true }).click();
  check('new experiment uses the API profile default', await page.getByLabel(parameterLabel, { exact: true }).inputValue() === String(profile.parameters.num_pts.default));
  await page.locator('form[data-experiment-draft]').getByLabel('Experiment name', { exact: true }).fill('Stereo feature budget');
  await page.getByLabel('Description', { exact: false }).fill('Pinned room1 recording and default tracking configuration.');
  await page.getByRole('button', { name: 'Create experiment', exact: true }).click();
  await page.getByRole('button', { name: 'Edit configuration', exact: true }).waitFor();
  const experimentId = new URL(page.url()).hash.split('/')[3];
  const experimentApi = `/projects/${projectId}/experiments/${experimentId}`;
  const baseline = (await apiRequest(page, experimentApi)).payload.revision;
  check('creating an experiment persists immutable version 1', baseline.version === 1 && baseline.specification.parameters.num_pts === 200);
  await page.getByRole('button', { name: 'Edit configuration', exact: true }).click();
  await page.getByLabel(parameterLabel, { exact: true }).selectOption('100');
  await page.getByRole('button', { name: 'Save new version', exact: true }).click();
  await page.getByRole('navigation', { name: 'Saved versions' }).getByRole('link', { name: /^Version 2/ }).waitFor();
  check('editing creates version 2 and exact parameter differences', await page.locator('#comparison-result').innerText().then(text => text.includes('parameters.num_pts') && text.includes('200') && text.includes('100')));
  const oldRevision = (await apiRequest(page, `${experimentApi}/revisions/1`)).payload.revision;
  assert.deepEqual(oldRevision.specification, baseline.specification);
  check('version 1 retains its exact original specification and hash', oldRevision.specification_sha256 === baseline.specification_sha256);
  await page.getByRole('button', { name: 'Edit configuration', exact: true }).click();
  await page.getByRole('button', { name: 'Save new version', exact: true }).click();
  await page.getByText('No changes to save. Version 2 is unchanged.', { exact: true }).waitFor();
  check('saving unchanged fields does not create a version', (await apiRequest(page, experimentApi)).payload.experiment.version === 2);
  await page.getByRole('navigation', { name: 'Saved versions' }).getByRole('link', { name: /^Version 1/ }).click();
  await page.getByText('Experiment · Historical version · read only', { exact: true }).waitFor();
  check('historical revision is read only and retains its saved value', await page.getByRole('button', { name: 'Edit configuration', exact: true }).count() === 0 && await page.locator('.parameter-value').innerText() === '200');
  await page.reload();
  await page.getByText('Experiment · Historical version · read only', { exact: true }).waitFor();
  check('historical version deep links survive refresh', new URL(page.url()).hash.endsWith('/revisions/1'));
  await page.getByRole('button', { name: 'Duplicate version 1', exact: true }).click();
  await page.getByRole('dialog').getByLabel('Experiment name', { exact: true }).fill('Reference configuration copy');
  await page.getByRole('button', { name: 'Duplicate experiment', exact: true }).click();
  await page.getByRole('heading', { name: 'Reference configuration copy', exact: true }).waitFor();
  const copiedId = new URL(page.url()).hash.split('/')[3];
  const copied = (await apiRequest(page, `/projects/${projectId}/experiments/${copiedId}`)).payload.revision;
  check('duplication uses the selected historical version and records provenance', copied.version === 1 && copied.specification_sha256 === baseline.specification_sha256 && copied.copied_from.experiment_id === experimentId && copied.copied_from.version === 1);
  await page.getByRole('button', { name: 'Delete experiment', exact: true }).click();
  await page.getByRole('button', { name: 'Keep experiment', exact: true }).click();
  check('experiment deletion has a cancellable confirmation', await page.getByRole('heading', { name: 'Reference configuration copy', exact: true }).isVisible());
  await page.getByRole('button', { name: 'Delete experiment', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete experiment', exact: true }).click();
  await page.locator(`a[href="#project/${projectId}/experiments/${experimentId}"]`).waitFor();
  check('deleting a copied experiment removes its revisions', (await apiRequest(page, `/projects/${projectId}/experiments/${copiedId}/revisions/1`)).status === 404);
  await page.locator(`a[href="#project/${projectId}/experiments/${experimentId}"]`).click();
  await page.getByRole('button', { name: 'Edit configuration', exact: true }).click();
  await page.locator('form[data-experiment-draft]').getByLabel('Experiment name', { exact: true }).fill('Feature budget comparison');
  await page.getByLabel('Description', { exact: false }).fill('Compare tracking density on the qualified room1 recording.');
  await page.getByLabel(parameterLabel, { exact: true }).selectOption('400');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('link', { name: 'Data', exact: true }).click();
  check('cancelling unsaved navigation keeps the experiment draft', await page.locator('form[data-experiment-draft]').getByLabel('Experiment name', { exact: true }).inputValue() === 'Feature budget comparison');
  const concurrent = await apiRequest(page, experimentApi, 'PATCH', { schema_version: 1, base_version: 2, name: 'Concurrent saved revision', description: 'Saved by another client.', parameters: { num_pts: 100 } });
  check('a concurrent client saves version 3', concurrent.status === 200 && concurrent.payload.experiment.version === 3);
  await page.getByRole('button', { name: 'Save new version', exact: true }).click();
  await page.getByRole('button', { name: 'Review latest version', exact: true }).waitFor();
  check('optimistic conflict preserves all draft fields', await page.locator('form[data-experiment-draft]').getByLabel('Experiment name', { exact: true }).inputValue() === 'Feature budget comparison' && await page.getByLabel(parameterLabel, { exact: true }).inputValue() === '400' && await page.getByLabel('Description', { exact: false }).inputValue() === 'Compare tracking density on the qualified room1 recording.');
  await page.getByRole('button', { name: 'Review latest version', exact: true }).click();
  await page.getByRole('button', { name: 'Use version 3 as base', exact: true }).click();
  check('adopting a newer base refreshes the displayed history while preserving the draft', await page.getByRole('navigation', { name: 'Saved versions' }).getByRole('link', { name: /^Version 3/ }).isVisible() && await page.locator('form[data-experiment-draft]').getByLabel('Experiment name', { exact: true }).inputValue() === 'Feature budget comparison' && await page.getByLabel(parameterLabel, { exact: true }).inputValue() === '400');
  await page.getByRole('button', { name: 'Save new version', exact: true }).click();
  await page.getByRole('navigation', { name: 'Saved versions' }).getByRole('link', { name: /^Version 4/ }).waitFor();
  const recovered = (await apiRequest(page, experimentApi)).payload;
  check('explicit conflict recovery saves the retained draft as version 4', recovered.experiment.version === 4 && recovered.revision.name === 'Feature budget comparison' && recovered.revision.specification.parameters.num_pts === 400);
  await page.getByLabel('From version', { exact: true }).selectOption('1');
  await page.getByText('Exact changes · v1 → v4', { exact: true }).waitFor();
  const changedFields = await page.locator('.comparison-table tbody th').allTextContents();
  check('comparison identifies every changed supported field', ['name', 'description', 'parameters.num_pts'].every(field => changedFields.includes(field)) && changedFields.length === 3);
  check('experiment launch availability matches the server capability', await page.getByRole('button', { name: /^Run version / }).count() === (executionEnabled ? 1 : 0));
  await page.screenshot({ path: `${output}experiment-desktop-dark.png`, fullPage: true });
  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  await page.screenshot({ path: `${output}experiment-desktop-light.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await noOverflow(page, 'experiment history and comparison fit a 390 px viewport');
  await page.screenshot({ path: `${output}experiment-mobile-light.png`, fullPage: true });
  await page.getByText('Immutable specification and provenance', { exact: true }).click();
  await noOverflow(page, 'expanded immutable specification fits a 390 px viewport');
  await page.getByText('Immutable specification and provenance', { exact: true }).click();
  check('private drafts are not persisted in browser storage', await page.evaluate(() => !JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]).includes('Feature budget comparison')));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByLabel('Appearance', { exact: true }).selectOption('dark');
  await page.getByRole('link', { name: 'Results', exact: true }).click();
  await page.getByRole('heading', { name: 'No results in this project' }).waitFor();
  await page.screenshot({ path: `${output}results-dark.png`, fullPage: true });
  check('project results never borrow the external qualification run', true);
  await page.getByRole('link', { name: 'Data', exact: true }).click();
  await page.getByRole('heading', { name: 'TUM-VI room1', exact: true }).waitFor();
  await page.getByLabel('Appearance', { exact: true }).selectOption('light');
  await page.screenshot({ path: `${output}dataset-light.png`, fullPage: true });
  await page.reload();
  await page.getByRole('heading', { name: 'TUM-VI room1', exact: true }).waitFor();
  check('light theme persists on reload', await page.locator('html').getAttribute('data-theme') === 'light');
  await page.setViewportSize({ width: 390, height: 844 });
  await noOverflow(page, 'data view has no horizontal overflow at 390 px');
  await page.getByText('Calibration, coordinates and timestamps', { exact: true }).click();
  await page.getByText('Source, license and reproducibility', { exact: true }).click();
  await noOverflow(page, 'expanded provenance has no horizontal overflow at 390 px');
  await page.screenshot({ path: `${output}dataset-mobile-light.png`, fullPage: true });
  await page.getByRole('link', { name: 'Projects', exact: true }).click();
  await page.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
  await noOverflow(page, 'project list has no horizontal overflow at 390 px');
  await page.screenshot({ path: `${output}projects-mobile-light.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: `${output}projects-light.png`, fullPage: true });
  await register(peer, peerUsername);
  await peer.goto(`${base}#project/${projectId}/data`);
  await peer.getByRole('heading', { name: 'Project unavailable', exact: true }).waitFor();
  check('a second account cannot open the first account project', !(await peer.locator('#app').innerText()).includes('VIO · Stereo reference'));
  const ownership = await peer.evaluate(async id => {
    const session = await (await fetch('/api/v1/session')).json();
    const results = [];
    for (const method of ['GET', 'PATCH', 'DELETE']) {
      const result = await fetch(`/api/v1/projects/${id}`, { method, headers: { 'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token }, ...(method === 'PATCH' ? { body: JSON.stringify({ name: 'Cross-account attempt' }) } : {}) });
      results.push(result.status);
    }
    return results;
  }, projectId);
  check('cross-account GET PATCH DELETE all return 404', ownership.every(status => status === 404));
  const foreignExperimentChecks = [
    [experimentApi, 'GET'],
    [`${experimentApi}/revisions`, 'GET'],
    [`${experimentApi}/revisions/1`, 'GET'],
    [`${experimentApi}/compare?from=1&to=4`, 'GET'],
    [experimentApi, 'PATCH', { schema_version: 1, base_version: 4, name: 'Unauthorized', description: '', parameters: { num_pts: 200 } }],
    [`${experimentApi}/duplicate`, 'POST', { schema_version: 1, source_version: 1, name: 'Unauthorized copy' }],
    [experimentApi, 'DELETE'],
  ];
  const foreignStatuses = [];
  for (const [path, method, body] of foreignExperimentChecks) foreignStatuses.push((await apiRequest(peer, path, method, body)).status);
  check('cross-account experiment reads mutations history comparison and duplication return 404', foreignStatuses.every(status => status === 404));
  const csrfCookie = (await context.cookies()).find(cookie => cookie.name === 'meridian_csrf');
  await context.addCookies([{ ...csrfCookie, value: 'a'.repeat(32) }]);
  await page.getByRole('button', { name: /New project/ }).click();
  await page.getByLabel('Project name', { exact: true }).fill('CSRF rejection must not create');
  await page.getByRole('button', { name: 'Create project', exact: true }).click();
  await page.getByRole('button', { name: 'Reload session', exact: true }).waitFor();
  check('real CSRF rejection is displayed with session reload recovery', true);
  await page.getByRole('button', { name: 'Reload session', exact: true }).click();
  await page.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
  check('CSRF rejection did not create a project', await page.locator('.project-row').count() === 1);
  await context.clearCookies();
  await page.locator('.project-row').click();
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor();
  check('missing cookie / unauthorized response clears private project content', !(await page.locator('#app').innerText()).includes('VIO · Stereo reference'));
  await page.getByLabel('Username', { exact: true }).fill(username);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('heading', { name: 'TUM-VI room1', exact: true }).waitFor();
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Delete project', exact: true }).click();
  await page.getByRole('button', { name: 'Keep project', exact: true }).click();
  check('delete confirmation can be cancelled', await page.getByRole('heading', { name: 'Project details', exact: true }).isVisible());
  await page.getByRole('button', { name: 'Delete project', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete project', exact: true }).click();
  await page.getByRole('heading', { name: 'No projects yet', exact: true }).waitFor();
  check('delete removes the live project', true);
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('heading', { name: 'Sign in', exact: true }).waitFor();
  const signedOut = await page.evaluate(async () => (await fetch('/api/v1/session')).json());
  check('logout invalidates the server session', signedOut.user === null);
  await page.setViewportSize({ width: 390, height: 844 });
  await noOverflow(page, 'sign-in view has no horizontal overflow at 390 px');
  await page.screenshot({ path: `${output}signin-mobile-light.png`, fullPage: true });
  await page.goto(new URL('/index.html', base).href);
  await page.getByRole('heading', { name: 'Roll & gyroscope bias.', exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('#load-status')?.hidden === true && document.querySelector('#estimate-bias')?.textContent.trim().length > 0);
  check('legacy explorer loads its recorded data', await page.locator('svg').count() > 0);
  await page.getByRole('link', { name: 'Parameter studies', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#studies-page')?.hidden === false && document.querySelector('#study-content')?.hidden === false);
  check('legacy explorer navigation remains functional', true);
  await page.screenshot({ path: `${output}explorer-mobile.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: `${output}explorer-desktop.png`, fullPage: true });
  check('no uncaught browser errors in workspace or explorer', pageErrors.length === 0);
  check('workspace and explorer have no CSP console errors', cspErrors.length === 0);
  check('all loaded web fonts are ready', await page.evaluate(async () => { await document.fonts.ready; return [...document.fonts].filter(font => font.status === 'error').length === 0; }));
} catch (error) {
  failure = String(error.message || error).replaceAll(password, '[redacted]');
  try { await page.screenshot({ path: `${output}failure.png`, fullPage: true }); } catch { /* Preserve the original failure. */ }
  process.exitCode = 1;
} finally {
  const report = {
    schema_version: 1,
    base,
    checked_at: new Date().toISOString(),
    passed: !failure,
    environment_description: process.env.MERIDIAN_QA_ENVIRONMENT || 'Not specified by the operator.',
    transport: targetUrl.protocol.replace(':', ''),
    execution_enabled: executionEnabled,
    created_accounts: createdAccounts,
    checks, pageErrors, cspErrors, consoleErrors, content_security_policy: contentSecurityPolicy,
    ...(failure ? { failure } : {}),
    limitations: [
      'Browser checks do not independently establish the database engine, service-restart persistence, deployment host or network topology.',
      targetUrl.protocol === 'https:' ? 'HTTPS certificate validation is enabled in the browser; this is not a complete TLS configuration audit.' : 'HTTP transport only; HTTPS and secure-cookie behavior are not validated.',
      'Unauthorized-session behavior is checked by removing cookies; actual server-side expiry requires a separate backend test.',
      'The two created QA accounts remain for administrator cleanup; this script does not delete accounts.',
    ],
  };
  await writeFile(`${output}report.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ passed: !failure, checks: checks.length, created_accounts: createdAccounts, pageErrors, cspErrors, output, ...(failure ? { failure } : {}) }, null, 2));
  await browser.close();
}
