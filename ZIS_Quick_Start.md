# Moved: see [QUICK_START.md](QUICK_START.md)

The ZIS and ZAF quick starts have been merged into a single guide: **[QUICK_START.md](QUICK_START.md)**.

The two halves are no longer separable. The ZIS flow bundle ships inside the ZAF app ZIP, and the app is what deploys it, so the sidebar app has to be installed and configured before the bundle can be deployed. Following either old guide on its own left you stuck partway.

Where the old steps went:

| Old step | Now |
|---|---|
| Steps 1 to 3 (OAuth clients, setup token, ZIS integration container) | **Step 1** |
| Step 4 (connect ZIS to TSANet) | **Step 2** |
| Step 5 (deploy the flow bundle, inbound webhook) | **Step 4**, after the app is installed in Step 3 |

Flow-level reference remains in [`zis/README.md`](zis/README.md). This file's previous content is in its git history.
