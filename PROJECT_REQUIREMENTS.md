Evidence Collection

1 Background / Problem
In audit and compliance engagements, auditors often must collect their own evidence directly from a client’s systems. They may receive read-only access to tools such as Workday, GitHub, NetSuite, Jira/Linear, etc. Evidence collection is frequently manual: auditors open links, take screenshots, record fields into spreadsheets, and download supporting artifacts.

We are building a General Browser Agent that can perform these workflows reliably across many systems, with consistent outputs suitable for audit documentation. It should be able to take in a user message stating the task and output anything from screenshots, csvs, natural language, or a mix of the above.

The product interface and authentication system is up for you to spec out, but mostly we will be evaluating based on the quality of the agent itself, design decisions, and number of successful tasks the agent is able to do.

You will be evaluated based on your design choices around how the agent operates, as well as its performance on the following tasks: Evidence Collection Project Evals and a separate hidden eval set. Of course, since we don’t expect you to have access to many of the enterprise ERPs, these are similar cases that aim to test the spirit of the audit use case.

Here are the tradeoffs we prioritize in the agent design:
Accuracy. How well can you get the agent to do each task?
Generability. How many of the tasks, plus other tasks can you do well?
Scalability to thousands of samples
Consistency between samples
Speed

