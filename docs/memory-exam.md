# The memory exam

A developer tool for checking changes to Auri's memory. It asks Auri questions it should answer from memory, as real requests, and has the backend grade the answers.

It isn't for households. Its questions cost what the same conversations would, so it's off for every account:
- **To turn it on for an account,** set `memory_exam` to `true` on its auth-clients row (see "Letting an account run the memory exam" in the AuriServiceCDK README). Then restart Home Assistant, or reload the integration.
- **Once memory connects,** the hub asks the backend whether its account may run the exam. Only then does `hey_auri_client.run_memory_exam` appear in Developer tools → Actions. Hubs on other accounts never get it.
- **The exam asks again before its first question,** so an account that has lost the exam since is refused before anything is asked.
- **Each question is a conversation of its own,** so the account isn't held to the backend's limit on conversation records. Its daily token limits still apply, and a day of runs can use up the household's allowance.

Ready-made exam files, the tools that read their results, and past results are in `AuriServiceTest/memory_exam`, next to this repository.

## Running it

To check how well memory works, create `hey_auri_memory_exam.yaml` in your configuration folder with questions and the answers you expect:

```yaml
- q: What day is Mia's piano lesson?
  expected: Thursday.
  memory: Mia piano
- q: Where are we going on vacation this summer?
  expected: Portugal, in July.
```

Then call `hey_auri_client.run_memory_exam`. Each question (up to 50) is asked in a new conversation, and the answers are graded. The result has a score from 0 to 1 and the grade for each question. Each question also shows:
- **`seconds`:** how long Auri took to answer.
- **`searches`:** how many times it searched its memory.
- **`search_results`,** when Auri searched: what each search returned. That's the words Auri searched for (`query`), how many memories came back (`results`) and how long they were (`chars`), and each memory's `score`. When the search computed them, the score's parts are shown too: `similarity`, how close the memory is in meaning, and `keyword`, how well its words match.
- **`embed_seconds`:** how long one call to turn the question into an embedding takes from your Home Assistant. A message that isn't in English waits for this call before Auri starts on it.
- **`meaning_notes`:** what a search by meaning alone finds, using the question's own words, in any language and with no limit on how close a memory has to be. It's measured after the answer, so it doesn't change it. It lists the closest memories. When the question names its memory, it also shows where that memory lands (`memory_rank`, empty if it isn't in the first 30) and how close it is (`memory_similarity`). Like the notes, it leaves out what's already in the profile, and private memories unless the household shares them. For a question that isn't in English, the memories at 0.60 or closer also reached Auri as notes.
- **`language_set`,** only on a question that made Auri change the household's language: the language it changed to. The exam puts your language back right away, so one question can't change what you hear at home.

Exam conversations aren't saved to memory. The questions run like real requests, so ask questions, not commands. They're asked without anyone signed in, as on a voice satellite. The result's `turns` section averages these over every question, the same way the `memory` section below does. So an exam file of plain questions, run on your real memory, still shows how its searches went.

Put times, dates, yes or no, and plain numbers in quotes (`expected: "20:00"`). Without quotes, YAML reads `20:00` as the number 1200 and `no` as false, so the exam refuses the file and names the question.

**Where an answer's memory came from.** `memory` is optional: a few words from the memory that answers the question. A memory matches when it has every word, and a word also matches its longer forms, so "lesson" matches "lessons". Each question then shows where that memory reached Auri, in `memory_found_in`:
- `profile`: the short profile of important facts Auri gets with every message;
- `notes`: the notes matched to the question;
- `recall_memory`: Auri had to search for it;
- `none`: it never reached Auri.

`memory_rank` is its place in that list (1 is first). The result's `memory` section adds them up, over the questions that name a memory:
- **`found`:** the share whose memory reached Auri.
- **`mrr`:** how high it came, from 0 to 1. It's the average of 1 divided by its place, counting 0 when it wasn't found. A memory in the profile counts as first, because the whole profile is sent with every message.
- **`seconds` and `searches`:** the averages per question. A case that needs Auri to search, such as an answer linked through a second memory, takes longer.
- **`results_per_search` and `chars_per_search`:** the averages per search, left out when nobody searched. A search that keeps coming back full and long is returning memories that don't help.
- **`embed_seconds`:** the average per question.
- **`not_correct`:** the answers that weren't correct.
  - `memory_shown`: Auri had the memory and still got it wrong.
  - `memory_not_shown`: the memory never reached Auri, so the search is what needs work.
- **`by_case`:** the same numbers for each `case`, an optional label you give questions, such as `paraphrase`.

**An outdated memory.** `outdated` is optional too: words from a memory the answer has replaced but that's still stored, such as an old address. Each question then also shows where that memory reached Auri, in `outdated_found_in`, with the same values as `memory_found_in`. The result's `outdated` section adds them up: how many still reached Auri (`shown`), where (`found_in`), and how many of those answers weren't correct (`not_correct_when_shown`).

**A test memory.** To test with memories you choose instead of your household's, list a household and memories in the file. Auri is asked about those, in a test memory built when the exam starts and deleted when it ends.
- **Your real memory is safe:** the exam's questions never read or change it, even one that makes Auri remember or forget something.
- **Runs compare:** the memories are the same every time.

```yaml
household:
  people: [Will, Mia]
  pets: [Biscuit]
memories:
  - text: Mia has piano every Thursday at 16:00.
    about: [Mia]
    importance: 4
  - text: Will has a dentist appointment at 10:00.
    about: [Will]
    kind: commitment
    due_in_days: 3
questions:
  - q: What day is Mia's piano lesson?
    expected: Thursday.
    memory: Mia piano
```

Each memory has `text`, and optionally:
- **`about`:** who or what it's about. Names that aren't under `household` are topics, such as a school or a place.
- **`kind`:** `fact` (the default), `preference`, `decision`, `commitment` or `digest` (the summary of a day).
- **`importance`:** from 1 to 5, 3 by default.
- **`private`:** `true` for a private memory. Private memories aren't in the notes for someone who isn't signed in, unless **Share memories within the household** is on.
- **`days_ago`:** when it was said, 0 by default.
- **`due_in_days`:** when a commitment is due.

The household can also list `same`: pairs of its pets that are one pet under two names, the right spelling first (`same: [[Zoey, Zoe]]`). That's the answer to the question in [One name, two spellings](../README.md#who-is-speaking), given before the questions are asked. The result's `similar_names` lists the pairs that question would ask about.

A test memory holds up to 300 memories. To keep more than one exam file, give the action's `file` option a file name from the config folder, for example `hey_auri_memory_benchmark.yaml`.

