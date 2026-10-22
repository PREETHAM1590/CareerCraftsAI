import assert from 'node:assert/strict';
import test from 'node:test';
import { hardenSupervisorDocker } from './harden-supervisor.mjs';

const upstream = `function hostConfig(options) {
  return {
    PidsLimit: options.pidsLimit ?? 512,
    ShmSize: 1,
  };
}`;

function hostConfigWith(cpus) {
  const env = cpus === undefined ? {} : { COMPUTER_CPUS: cpus };
  const source = `${hardenSupervisorDocker(upstream)}\nreturn hostConfig;`;
  return new Function('process', source)({ env })({});
}

test('COMPUTER_CPUS caps each browser container', () => {
  assert.equal(hostConfigWith('1.5').NanoCpus, 1_500_000_000);
  assert.equal(hostConfigWith('1.5').PidsLimit, 512);
  assert.equal('NanoCpus' in hostConfigWith(undefined), false);
});

test('a bad COMPUTER_CPUS refuses to create the container', () => {
  for (const bad of ['0', '-1', 'two', '65', '1e-10', '0.009']) assert.throws(() => hostConfigWith(bad), /COMPUTER_CPUS/);
});

test('a changed upstream contract fails the build', () => {
  assert.throws(() => hardenSupervisorDocker('PidsLimit: 1,'), /contract changed/);
});
