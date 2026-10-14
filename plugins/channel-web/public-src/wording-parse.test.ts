import { describe, expect, it } from 'vitest';

import { wordingOf } from '@/wording-parse';

describe('wordingOf', () => {
  it('reads an absent catalogue as null', () => {
    expect(wordingOf(undefined)).toBeNull();
    expect(wordingOf(null)).toBeNull();
  });

  it('rejects a non-record', () => {
    expect(wordingOf('nope')).toBeUndefined();
    expect(wordingOf([1, 2])).toBeUndefined();
  });

  it('rejects an unknown chapter', () => {
    expect(wordingOf({ bogus: {} })).toBeUndefined();
  });

  it('parses the form chapter down to the web-drawn members', () => {
    expect(
      wordingOf({
        form: {
          title: 'Ignored on web',
          open: 'Ignored',
          submit: 'File it',
          back: 'Previous',
          fields: { kind: { choice_label: 'Pick a kind' } },
        },
      }),
    ).toEqual({
      form: {
        submit: 'File it',
        back: 'Previous',
        fields: { kind: { choiceLabel: 'Pick a kind' } },
      },
      choice: null,
      answer: null,
      status: null,
    });
  });

  it('defaults absent form members to null and an absent field map to empty', () => {
    expect(wordingOf({ form: { submit: 'Go' } })).toEqual({
      form: { submit: 'Go', back: null, fields: {} },
      choice: null,
      answer: null,
      status: null,
    });
  });

  it('rejects an unknown form key, a blank member, and a bad field map', () => {
    expect(wordingOf({ form: { nope: 'x' } })).toBeUndefined();
    expect(wordingOf({ form: { submit: '   ' } })).toBeUndefined();
    expect(wordingOf({ form: { title: '' } })).toBeUndefined();
    expect(wordingOf({ form: { fields: [] } })).toBeUndefined();
    expect(wordingOf({ form: { fields: { kind: { bogus: 'x' } } } })).toBeUndefined();
    expect(wordingOf({ form: { fields: { kind: { choice_label: '' } } } })).toBeUndefined();
  });

  it('parses the choice chapter down to the group label', () => {
    expect(wordingOf({ choice: { label: 'Ignored', group_label: 'Pick one' } })).toEqual({
      form: null,
      choice: { groupLabel: 'Pick one' },
      answer: null,
      status: null,
    });
  });

  it('rejects an unknown choice key and a blank member', () => {
    expect(wordingOf({ choice: { nope: 'x' } })).toBeUndefined();
    expect(wordingOf({ choice: { group_label: ' ' } })).toBeUndefined();
  });

  it('parses the answer chapter, dropping only rejected_final', () => {
    expect(
      wordingOf({
        answer: {
          open: 'Open',
          confirm: 'Yes',
          decline: 'No',
          submit: 'Send',
          reply_hint: 'Type',
          rejected: 'Nope: {reason}',
          rejected_final: 'Closed',
        },
      }),
    ).toEqual({
      form: null,
      choice: null,
      answer: {
        open: 'Open',
        confirm: 'Yes',
        decline: 'No',
        submit: 'Send',
        replyHint: 'Type',
        rejected: 'Nope: {reason}',
      },
      status: null,
    });
  });

  it('rejects an unknown answer key and a blank member', () => {
    expect(wordingOf({ answer: { nope: 'x' } })).toBeUndefined();
    expect(wordingOf({ answer: { rejected_final: '' } })).toBeUndefined();
    expect(wordingOf({ answer: { confirm: '  ' } })).toBeUndefined();
  });

  it('parses the status chapter, including the countdown sub-chapter', () => {
    expect(
      wordingOf({
        status: {
          sending: 'Sending',
          answered: 'Done',
          expired: 'Too late',
          closed: 'Ended',
          sent: 'Submitted',
          unavailable: 'Gone',
          countdown: {
            seconds_left: '{seconds} s left',
            one_second_left: '1 s left',
            under_minute: '< 1 min',
            under_half_minute: '< 30 s',
            under_ten_seconds: '< 10 s',
          },
        },
      }),
    ).toEqual({
      form: null,
      choice: null,
      answer: null,
      status: {
        sending: 'Sending',
        answered: 'Done',
        expired: 'Too late',
        closed: 'Ended',
        sent: 'Submitted',
        unavailable: 'Gone',
        countdown: {
          secondsLeft: '{seconds} s left',
          oneSecondLeft: '1 s left',
          underMinute: '< 1 min',
          underHalfMinute: '< 30 s',
          underTenSeconds: '< 10 s',
        },
      },
    });
  });

  it('rejects a seconds_left line without the {seconds} token', () => {
    expect(wordingOf({ status: { countdown: { seconds_left: 'no token' } } })).toBeUndefined();
  });

  it('rejects an unknown status or countdown key and a blank member', () => {
    expect(wordingOf({ status: { nope: 'x' } })).toBeUndefined();
    expect(wordingOf({ status: { countdown: { nope: 'x' } } })).toBeUndefined();
    expect(wordingOf({ status: { answered: '' } })).toBeUndefined();
    expect(wordingOf({ status: { countdown: 5 } })).toBeUndefined();
  });

  it('accepts the message-line and answer-page chapters as records it draws nothing for', () => {
    expect(
      wordingOf({
        lines: { deadline: 'by {deadline}' },
        answer_page: { done: { heading: 'Done' } },
      }),
    ).toEqual({ form: null, choice: null, answer: null, status: null });
  });

  it('rejects a non-record lines or answer_page chapter', () => {
    expect(wordingOf({ lines: 'nope' })).toBeUndefined();
    expect(wordingOf({ answer_page: 7 })).toBeUndefined();
  });
});
