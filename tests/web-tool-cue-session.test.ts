import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../src/web-client.ts', import.meta.url), 'utf8');

function pageFunction(name: string): string {
	const start = source.indexOf(`function ${name}(`);
	assert.notEqual(start, -1, `${name} must exist in the served page`);
	const end = source.indexOf('\n}', start);
	assert.notEqual(end, -1, `${name} must have a closing brace`);
	return source.slice(start, end + 2);
}

type Session = {
	connected: boolean;
	voice: { connected: boolean } | null;
	contextState?: 'running' | 'suspended';
	muted?: boolean;
};

function harness(session: Session) {
	const counts = { contextsCreated: 0, resumes: 0 };
	const tones: number[] = [];
	const listeners = new Map<string, (event: { data: string }) => void>();
	class FakeAudioContext {
		state = 'running';
		currentTime = 0;
		destination = {};
		constructor() { counts.contextsCreated++; }
		resume() { counts.resumes++; return Promise.resolve(); }
		createOscillator() {
			const frequency = { value: 0 };
			return {
				frequency,
				connect() {},
				start() { tones.push(frequency.value); },
				stop() {},
			};
		}
		createGain() {
			return {
				gain: {
					setValueAtTime() {},
					linearRampToValueAtTime() {},
					exponentialRampToValueAtTime() {},
				},
				connect() {},
			};
		}
	}
	class FakeEventSource {
		addEventListener(name: string, listener: (event: { data: string }) => void) {
			listeners.set(name, listener);
		}
		close() {}
	}
	const audioCtx = session.contextState ? new FakeAudioContext() : null;
	if (audioCtx) audioCtx.state = session.contextState!;
	counts.contextsCreated = 0;
	new Function('AudioContext', 'EventSource', 'session', 'initialAudioCtx', `
		let connected = session.connected;
		let voice = session.voice;
		let muted = session.muted || false;
		let audioCtx = initialAudioCtx;
		let _sseSource = null;
		${pageFunction('playToolCue')}
		${pageFunction('initRemoteToggle')}
		initRemoteToggle();
	`)(FakeAudioContext, FakeEventSource, session, audioCtx);
	return {
		counts,
		tones,
		cue(kind: string) {
			const listener = listeners.get('tool-cue');
			assert.ok(listener, 'the shipped SSE listener must receive tool cues');
			listener({ data: kind });
		},
	};
}

const inactiveCases: [string, Session][] = [
	['a passive tab that never started voice', { connected: false, voice: null }],
	['an ended call with a retained running context', { connected: false, voice: null, contextState: 'running' }],
	['an ended call with a retained suspended context', { connected: false, voice: null, contextState: 'suspended' }],
	['a connection attempt before the voice socket opens', { connected: true, voice: { connected: false }, contextState: 'running' }],
	['a requested call without a voice transport', { connected: true, voice: null, contextState: 'running' }],
	['a stopped surface with a stale open transport', { connected: false, voice: { connected: true }, contextState: 'running' }],
];

for (const [name, session] of inactiveCases) {
	test(`tool cues stay silent in ${name}`, () => {
		const h = harness(session);
		h.cue('research');
		assert.deepEqual(h.tones, [], 'an inactive tab must not schedule audible tones');
		assert.equal(h.counts.contextsCreated, 0, 'an inactive tab must not create an audio context');
		assert.equal(h.counts.resumes, 0, 'an inactive tab must not resume an audio context');
	});
}

const activeCases: [string, number[]][] = [
	['tool', [820]],
	['research', [720, 1080]],
	['work', [500]],
];

for (const [kind, expected] of activeCases) {
	test(`an active voice call preserves the ${kind} cue`, () => {
		const h = harness({ connected: true, voice: { connected: true }, contextState: 'running' });
		h.cue(kind);
		assert.deepEqual(h.tones, expected);
		assert.equal(h.counts.contextsCreated, 0, 'the active call reuses its cue context');
	});
}

test('microphone mute preserves output cues for an active voice call', () => {
	const h = harness({ connected: true, voice: { connected: true }, contextState: 'running', muted: true });
	h.cue('work');
	assert.deepEqual(h.tones, [500]);
});
