import 'package:flutter/material.dart';
import 'package:kitchenowl/kitchenowl.dart';
import 'package:kitchenowl/models/household.dart';
import 'package:kitchenowl/services/api/api_service.dart';

typedef LltRequest = ({String name, String scope, int? householdId});

class LltCreateDialog extends StatefulWidget {
  const LltCreateDialog({super.key});

  @override
  State<LltCreateDialog> createState() => _LltCreateDialogState();
}

class _LltCreateDialogState extends State<LltCreateDialog> {
  final TextEditingController nameController = TextEditingController();
  String scope = 'write';
  int? householdId;
  List<Household>? households;

  @override
  void initState() {
    super.initState();
    nameController.addListener(() => setState(() {}));
    ApiService.getInstance().getAllHouseholds().then((value) {
      if (mounted) setState(() => households = value);
    });
  }

  @override
  void dispose() {
    nameController.dispose();
    super.dispose();
  }

  String _scopeDescription(BuildContext context) => switch (scope) {
        'read' => AppLocalizations.of(context)!.lltScopeReadDescription,
        'full' => AppLocalizations.of(context)!.lltScopeFullDescription,
        _ => AppLocalizations.of(context)!.lltScopeWriteDescription,
      };

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text(AppLocalizations.of(context)!.lltCreate),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            TextField(
              controller: nameController,
              autofocus: true,
              decoration: InputDecoration(
                hintText: AppLocalizations.of(context)!.name,
              ),
            ),
            const SizedBox(height: 16),
            Text(
              AppLocalizations.of(context)!.lltScope,
              style: Theme.of(context).textTheme.labelLarge,
            ),
            const SizedBox(height: 4),
            DropdownButtonFormField<String>(
              value: scope,
              items: [
                DropdownMenuItem(
                  value: 'read',
                  child: Text(AppLocalizations.of(context)!.lltScopeRead),
                ),
                DropdownMenuItem(
                  value: 'write',
                  child: Text(AppLocalizations.of(context)!.lltScopeWrite),
                ),
                DropdownMenuItem(
                  value: 'full',
                  child: Text(AppLocalizations.of(context)!.lltScopeFull),
                ),
              ],
              onChanged: (value) {
                if (value != null) setState(() => scope = value);
              },
            ),
            const SizedBox(height: 4),
            Text(
              _scopeDescription(context),
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 16),
            Text(
              AppLocalizations.of(context)!.lltHousehold,
              style: Theme.of(context).textTheme.labelLarge,
            ),
            const SizedBox(height: 4),
            DropdownButtonFormField<int?>(
              value: householdId,
              items: [
                DropdownMenuItem(
                  value: null,
                  child: Text(AppLocalizations.of(context)!.lltHouseholdAll),
                ),
                for (final household in households ?? const <Household>[])
                  DropdownMenuItem(
                    value: household.id,
                    child: Text(household.name),
                  ),
              ],
              onChanged: (value) => setState(() => householdId = value),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: Text(AppLocalizations.of(context)!.cancel),
        ),
        TextButton(
          onPressed: nameController.text.trim().isEmpty
              ? null
              : () => Navigator.of(context).pop((
                    name: nameController.text.trim(),
                    scope: scope,
                    householdId: householdId,
                  )),
          child: Text(AppLocalizations.of(context)!.add),
        ),
      ],
    );
  }
}
