/**
 * The voice system instruction must name the host OS the agent is actually on.
 *
 * It used to hardcode "your local Mac". On Windows that was the only statement
 * about the host the model ever received, and the sole correction was the
 * "Running on win32" text inside macOSOnlyError — so the model assumed macOS
 * until some tool call happened to fail, then switched mid-session.
 */
import { test } from 'node:test';
import assert from 'node:assert';
import { buildInstructions } from '../src/voice-agent-config.js';

const ctx = {
	resolveCurrentMode: () => ({ marker: '' }) as never,
	isMeetingActive: () => false,
	googleSearch: false,
	resetSessionGates() {},
	resetNoteViewingDebounce() {},
	getRecentConversation: () => '',
	getSecondsSinceLastTurn: () => null,
};

const EXPECTED: Record<string, string> = { win32: 'Windows PC', linux: 'Linux machine', darwin: 'Mac' };

test('names the machine this process is actually running on', () => {
	const expected = EXPECTED[process.platform] ?? 'Mac';
	const instructions = buildInstructions(ctx);
	assert.ok(instructions.includes(`local ${expected}`),
		`instructions must say "local ${expected}" on ${process.platform}`);
	for (const [platform, machine] of Object.entries(EXPECTED)) {
		if (platform === process.platform || machine === expected) continue;
		assert.ok(!instructions.includes(`local ${machine}`),
			`must not claim "local ${machine}" while on ${process.platform}`);
	}
});

test('anchors can pin the machine so snapshots survive a different runner', () => {
	assert.match(buildInstructions(ctx, { ownerMachine: 'Mac' }), /local Mac\b/);
	assert.match(buildInstructions(ctx, { ownerMachine: 'Windows PC' }), /local Windows PC\b/);
});
