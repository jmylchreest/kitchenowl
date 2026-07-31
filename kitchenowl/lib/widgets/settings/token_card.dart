import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:kitchenowl/kitchenowl.dart';
import 'package:kitchenowl/models/token.dart';
import 'package:kitchenowl/widgets/settings/token_bottom_sheet.dart';

class TokenCard extends StatelessWidget {
  final Token token;
  final void Function()? onLogout;
  final bool enableOnTap;

  const TokenCard({
    super.key,
    required this.token,
    this.onLogout,
    this.enableOnTap = true,
  });

  String? _scopeLabel(BuildContext context) => switch (token.scope) {
        'read' => AppLocalizations.of(context)!.lltScopeRead,
        'write' => AppLocalizations.of(context)!.lltScopeWrite,
        'full' => AppLocalizations.of(context)!.lltScopeFull,
        _ => null,
      };

  @override
  Widget build(BuildContext context) {
    final lastUsed = token.lastUsedAt ?? token.createdAt;
    final details = [
      if (lastUsed != null)
        "${AppLocalizations.of(context)!.lastUsed}: ${DateFormat.yMMMEd().add_jm().format(lastUsed)}",
      if (_scopeLabel(context) != null)
        "${AppLocalizations.of(context)!.lltScope}: ${_scopeLabel(context)}",
    ];

    final child = Card(
      child: ListTile(
        title: Text(
          token.name,
        ),
        subtitle: details.isNotEmpty ? Text(details.join('\n')) : null,
        isThreeLine: details.length > 1,
        onTap: enableOnTap
            ? () => showModalBottomSheet(
                  context: context,
                  showDragHandle: true,
                  builder: (context) => TokenBottomSheet(
                    token: token,
                    onLogout: onLogout,
                  ),
                )
            : null,
      ),
    );

    if (onLogout == null) return child;

    return Dismissible(
      key: ValueKey<Token>(token),
      confirmDismiss: (direction) async {
        return (await askForConfirmation(
          context: context,
          title: Text(
            AppLocalizations.of(context)!.lltDelete,
          ),
          content: Text(
            AppLocalizations.of(context)!.lltDeleteConfirmation(
              token.name,
            ),
          ),
        ));
      },
      onDismissed: (_) => onLogout!(),
      background: Container(
        alignment: Alignment.centerLeft,
        padding: const EdgeInsets.only(left: 16),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(14),
          color: Colors.redAccent,
        ),
        child: const Icon(
          Icons.delete,
          color: Colors.white,
        ),
      ),
      secondaryBackground: Container(
        alignment: Alignment.centerRight,
        padding: const EdgeInsets.only(right: 16),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(14),
          color: Colors.redAccent,
        ),
        child: const Icon(
          Icons.delete,
          color: Colors.white,
        ),
      ),
      child: child,
    );
  }
}
