#!/usr/bin/env node
/**
 * Sutando — Cross-platform task runner backed by GitHub Copilot CLI.
 *
 * Polls `tasks/*.txt` (default 500ms) for new task files. For each one,
 * runs Copilot CLI with a short fixed wrapper prompt that instructs the
 * agent to READ the task file from disk (avoiding all shell-escape issues
 * with arbitrary user content). Captures stdout, writes `results/<id>.txt`
 * atomically, then archives the task file.
 *
 * Cross-platform: handles Windows .cmd shims (npm-installed `copilot` is
 * `copilot.cmd`) by spawning through `cmd.exe /c` with arg-array passing
 * (preserves arg boundaries, no shell:true escaping risk).
 *
 * Replaces the Mac-only `watch-tasks.sh` + `start-cli.sh` (claude tmux
 * session) duo with a single Node script that needs no npm dependencies
 * (uses Node stdlib only — `npm install` is not required on Windows).
 *
 * Design notes:
 *   - Polling, not fs.watch — fs.watch on Windows misses events, fires
 *     before writes complete, behaves differently on network drives, etc.
 *     Throughput is tiny so polling at 500ms is fine.
 *   - Stable-mtime check (file size & mtime stable for 2 polls) avoids
 *     reading partially-written task files.
 *   - Serial execution — one Copilot subprocess at a time. Prevents
 *     concurrent CLI sessions from colliding.
 *   - Per-task timeout (default 10 min, env COPILOT_TASK_TIMEOUT_MS).
 *   - Always writes a result file even on failure so the web UI never
 *     polls forever.
 *   - Atomic writes via temp-then-rename (Windows-safe).
 *   - Wrapper prompt is fixed/simple — task content lives in the file
 *     Copilot reads, so no special chars ever hit the shell.
 *
 * Usage:
 *   node src/copilot-task-runner.mjs            # poll forever
 *   node src/copilot-task-runner.mjs --once     # process all pending then exit
 */

import { spawn } from 'node:child_process';
import {
	mkdirSync,
	readdirSync,
	readFileSync,
	writeFileSync,
	statSync,
	renameSync,
	existsSync,
	unlinkSync,
} from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_DIR = join(dirname(__filename), '..');
const TASK_DIR = join(REPO_DIR, 'tasks');
const RESULT_DIR = join(REPO_DIR, 'results');
const LOG_DIR = join(REPO_DIR, 'logs');

const POLL_INTERVAL_MS = parseInt(process.env.COPILOT_POLL_INTERVAL_MS || '500', 10);
const TASK_TIMEOUT_MS = parseInt(process.env.COPILOT_TASK_TIMEOUT_MS || '600000', 10); // 10 min
const COPILOT_BIN = process.env.COPILOT_BIN || 'copilot';
const RUN_ONCE = process.argv.includes('--once');
const IS_WIN = process.platform === 'win32';

mkdirSync(TASK_DIR, { recursive: true });
mkdirSync(RESULT_DIR, { recursive: true });
mkdirSync(LOG_DIR, { recursive: true });

function ts() {
	return new Date().toISOString().replace('T', ' ').slice(0, 19);
}

function log(msg) {
	console.log(`${ts()} [task-runner] ${msg}`);
}

/** Atomic write: write to .tmp then rename. Safe on Windows. */
function atomicWrite(filePath, content) {
	const tmp = filePath + '.tmp';
	writeFileSync(tmp, content);
	renameSync(tmp, filePath);
}

/** Track size+mtime per task file across polls so we don't read partially
 * written files. Stable when size+mtime match for two consecutive polls. */
const seenStats = new Map(); // filename → { size, mtimeMs, stableCount }
const inFlight = new Set();
const archived = new Set();

function isStable(filename, st) {
	const prev = seenStats.get(filename);
	const cur = { size: st.size, mtimeMs: st.mtimeMs, stableCount: 1 };
	if (prev && prev.size === st.size && prev.mtimeMs === st.mtimeMs) {
		cur.stableCount = prev.stableCount + 1;
	}
	seenStats.set(filename, cur);
	return cur.stableCount >= 2;
}

function archiveTask(filename) {
	const src = join(TASK_DIR, filename);
	if (!existsSync(src)) return;
	try {
		const ym = new Date().toISOString().slice(0, 7);
		const destDir = join(TASK_DIR, 'archive', ym);
		mkdirSync(destDir, { recursive: true });
		renameSync(src, join(destDir, filename));
	} catch (err) {
		log(`archive failed for ${filename}: ${err}; deleting instead`);
		try { unlinkSync(src); } catch { /* ignore */ }
	}
}

/** Build the wrapper prompt sent to Copilot. Deliberately simple — no shell
 * metacharacters and no user-controlled content. The full task file
 * (including any in-band system instructions) is read by Copilot via its
 * Read tool from disk, not passed through CLI args. */
function buildPrompt(absoluteTaskPath) {
	return [
		'You are Sutando, a personal AI assistant running locally for the user.',
		`The user submitted a task. Read the task file at ${absoluteTaskPath} for the full content.`,
		'Process the task. Reply with ONLY the answer text — no preamble like "Sure!" or "Here is".',
		'Keep the reply concise and conversational; it will be read aloud via text-to-speech.',
		'If the task is a question, answer it directly.',
		'If the task asks you to do something on the system, do it, then briefly confirm.',
		'Do not include the task file metadata (id:, timestamp:, source:, from:) in your reply.',
	].join(' ');
}

/** Resolve the Copilot CLI invocation on Windows. There can be multiple
 * copies on PATH — VS Code ships a `copilot.bat` shim that re-invokes
 * PowerShell (`powershell -File copilot.ps1 %*`), which mangles long prompt
 * args because PowerShell's `-File` arg parser re-tokenises everything. The
 * npm-installed `copilot.cmd` is a proper Node shim (`node ...loader.js %*`)
 * that preserves args. We bypass the shim entirely and call Node with the
 * loader script directly — no cmd.exe, no shell, no quoting headaches. */
function resolveCopilotInvocation() {
	if (!IS_WIN) return { command: COPILOT_BIN, prefixArgs: [], useShell: false };

	const candidates = [];
	const pathDirs = (process.env.PATH || '').split(';').filter(Boolean);
	if (COPILOT_BIN.toLowerCase().endsWith('.cmd') || COPILOT_BIN.includes('\\') || COPILOT_BIN.includes('/')) {
		candidates.push(COPILOT_BIN);
	}
	for (const dir of pathDirs) {
		for (const ext of ['.cmd', '.bat', '.exe']) {
			const p = join(dir, COPILOT_BIN + ext);
			if (existsSync(p)) candidates.push(p);
		}
	}
	const cmdShim = candidates.find((p) => p.toLowerCase().endsWith('.cmd'));
	if (cmdShim) {
		const dir = dirname(cmdShim);
		const loaderRel = join('node_modules', '@github', 'copilot', 'npm-loader.js');
		const loaderAbs = join(dir, loaderRel);
		const localNode = join(dir, 'node.exe');
		if (existsSync(loaderAbs)) {
			const node = existsSync(localNode) ? localNode : 'node';
			return { command: node, prefixArgs: [loaderAbs], useShell: false };
		}
	}
	const exe = candidates.find((p) => p.toLowerCase().endsWith('.exe'));
	if (exe) return { command: exe, prefixArgs: [], useShell: false };
	if (cmdShim) return { command: cmdShim, prefixArgs: [], useShell: true };
	return { command: COPILOT_BIN, prefixArgs: [], useShell: false };
}

const COPILOT_INVOCATION = resolveCopilotInvocation();

function spawnCopilot(args, options) {
	const allArgs = [...COPILOT_INVOCATION.prefixArgs, ...args];
	if (COPILOT_INVOCATION.useShell) {
		return spawn(COPILOT_INVOCATION.command, allArgs, { ...options, shell: true });
	}
	return spawn(COPILOT_INVOCATION.command, allArgs, options);
}

/** Run copilot for one task. Always writes results/<id>.txt — success or
 * failure — so the polling UI never hangs. */
async function runCopilot(taskId, taskFilePath) {
	const prompt = buildPrompt(taskFilePath);
	const args = [
		'-p', prompt,
		'--allow-all-tools',
		'--no-ask-user',
		'--add-dir', REPO_DIR,
		'-s',
	];

	log(`[${taskId}] spawning copilot (timeout ${Math.round(TASK_TIMEOUT_MS / 1000)}s)`);

	return new Promise((resolve) => {
		let stdout = '';
		let stderr = '';
		let timedOut = false;
		let settled = false;

		const settle = (val) => {
			if (settled) return;
			settled = true;
			resolve(val);
		};

		const child = spawnCopilot(args, {
			cwd: REPO_DIR,
			env: process.env,
			windowsHide: true,
		});

		const timer = setTimeout(() => {
			timedOut = true;
			log(`[${taskId}] TIMEOUT after ${TASK_TIMEOUT_MS}ms — killing copilot`);
			try { child.kill('SIGKILL'); } catch { /* ignore */ }
		}, TASK_TIMEOUT_MS);

		child.stdout.on('data', (d) => { stdout += d.toString(); });
		child.stderr.on('data', (d) => { stderr += d.toString(); });

		child.on('error', (err) => {
			clearTimeout(timer);
			const msg = `Failed to spawn copilot: ${err.message}\n` +
				`Make sure GitHub Copilot CLI is installed and on PATH ` +
				`(see https://docs.github.com/copilot/how-tos/copilot-cli).`;
			log(`[${taskId}] spawn error: ${err}`);
			settle({ ok: false, text: msg });
		});

		child.on('close', (code) => {
			clearTimeout(timer);
			const out = stdout.trim();
			if (timedOut) {
				settle({ ok: false, text: `Task timed out after ${Math.round(TASK_TIMEOUT_MS / 1000)} seconds. Partial output:\n\n${out || '(none)'}` });
				return;
			}
			if (code !== 0) {
				const errMsg = `Copilot exited with code ${code}.\n\nSTDERR:\n${stderr.trim() || '(empty)'}\n\nSTDOUT:\n${out || '(empty)'}`;
				log(`[${taskId}] non-zero exit ${code}`);
				settle({ ok: false, text: errMsg });
				return;
			}
			if (!out) {
				settle({ ok: false, text: `Copilot returned empty output. STDERR: ${stderr.trim() || '(none)'}` });
				return;
			}
			settle({ ok: true, text: out });
		});
	});
}

async function processTask(filename) {
	if (inFlight.has(filename) || archived.has(filename)) return;
	inFlight.add(filename);
	const fullPath = join(TASK_DIR, filename);
	const taskId = filename.replace(/\.txt$/, '');
	try {
		const size = statSync(fullPath).size;
		log(`[${taskId}] processing (${size} bytes)`);
		const { ok, text } = await runCopilot(taskId, fullPath);
		const resultPath = join(RESULT_DIR, filename);
		atomicWrite(resultPath, text);
		log(`[${taskId}] result written (${text.length} chars, ${ok ? 'ok' : 'FAILED'})`);
	} catch (err) {
		log(`[${taskId}] processTask error: ${err.stack || err}`);
		try {
			atomicWrite(join(RESULT_DIR, filename), `Internal error: ${err.message || err}`);
		} catch { /* ignore */ }
	} finally {
		archiveTask(filename);
		archived.add(filename);
		seenStats.delete(filename);
		inFlight.delete(filename);
	}
}

/** Serial queue — one Copilot subprocess at a time. */
let processing = false;
const queue = [];

function enqueue(filename) {
	if (queue.includes(filename) || inFlight.has(filename) || archived.has(filename)) return;
	queue.push(filename);
	pump();
}

async function pump() {
	if (processing) return;
	processing = true;
	while (queue.length > 0) {
		const filename = queue.shift();
		await processTask(filename);
	}
	processing = false;
}

function pollOnce() {
	let entries;
	try {
		entries = readdirSync(TASK_DIR);
	} catch (err) {
		log(`readdir error: ${err}`);
		return;
	}
	for (const name of entries) {
		if (!name.endsWith('.txt')) continue;
		if (archived.has(name)) continue;
		const full = join(TASK_DIR, name);
		let st;
		try { st = statSync(full); } catch { continue; }
		if (!st.isFile()) continue;
		if (!isStable(name, st)) continue;
		enqueue(name);
	}
}

async function main() {
	log(`Sutando task runner starting`);
	log(`  TASK_DIR    = ${TASK_DIR}`);
	log(`  RESULT_DIR  = ${RESULT_DIR}`);
	const inv = COPILOT_INVOCATION;
	const desc = inv.prefixArgs.length
		? `${inv.command} ${inv.prefixArgs.join(' ')}`
		: inv.command;
	log(`  COPILOT_BIN = ${COPILOT_BIN} → ${desc}${inv.useShell ? ' (via shell)' : ''}`);
	log(`  POLL        = ${POLL_INTERVAL_MS}ms, TIMEOUT = ${TASK_TIMEOUT_MS}ms`);

	if (RUN_ONCE) {
		// Need two polls so isStable() returns true on existing files.
		pollOnce();
		await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS + 50));
		pollOnce();
		while (queue.length > 0 || processing) {
			await new Promise((r) => setTimeout(r, 100));
		}
		log('One-shot sweep complete; exiting.');
		return;
	}

	setInterval(pollOnce, POLL_INTERVAL_MS);
	process.on('SIGINT', () => { log('SIGINT received, exiting'); process.exit(0); });
	process.on('SIGTERM', () => { log('SIGTERM received, exiting'); process.exit(0); });
}

main().catch((err) => {
	log(`Fatal: ${err.stack || err}`);
	process.exit(1);
});

