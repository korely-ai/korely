# Additional terms for the local embeddings: Gemma

These terms come with Korely Agent next to the Apache License 2.0 in
[LICENSE](../LICENSE), which they do not modify: they concern a third-party
model the software can download, not the software itself. In them, "Customer"
is whoever uses Korely Agent, "Software" is Korely Agent, and "this Agreement"
is the licence or contract under which Customer received it.

**Third-party model: Gemma.** When local embeddings are enabled, the Software
downloads and runs EmbeddingGemma, a model made by Google LLC. Gemma is
provided under and subject to the Gemma Terms of Use found at
ai.google.dev/gemma/terms (the "Gemma Terms"), a copy of which is provided with
the Software. Customer's use of the model is governed by the Gemma Terms.
Customer shall not use the model, or the Software to the extent it runs the
model, (a) for any use restricted by the Gemma Prohibited Use Policy at
ai.google.dev/gemma/prohibited_use_policy, which is incorporated into this
Agreement by reference, or (b) in violation of applicable laws and regulations.
Google may restrict use of the model that it reasonably believes violates the
Gemma Terms. Korely grants no rights in the model beyond those Customer
receives under the Gemma Terms. If Customer makes the model or its
functionality available to anyone else, Customer shall impose the same
restrictions on them.

The copy of the Gemma Terms: [gemma-terms-of-use.txt](gemma-terms-of-use.txt).
Local embeddings are the default since version 0.1.19 (`EMBEDDING_PROVIDER=local`):
the image is built with the model, and the build puts these terms, the copy and
the Gemma notice next to the weights. A build with `EMBEDDING_PROVIDER=gemini`
downloads no model and runs none.
