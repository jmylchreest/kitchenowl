import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_bloc/flutter_bloc.dart';
import 'package:kitchenowl/cubits/settings_user_cubit.dart';
import 'package:kitchenowl/enums/token_type_enum.dart';
import 'package:kitchenowl/kitchenowl.dart';
import 'package:kitchenowl/widgets/settings/llt_create_dialog.dart';
import 'package:kitchenowl/widgets/settings/token_card.dart';
import 'package:share_plus/share_plus.dart';

class SettingsUserSessionsPage extends StatelessWidget {
  final SettingsUserCubit cubit;

  const SettingsUserSessionsPage({super.key, required this.cubit});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(AppLocalizations.of(context)!.sessions),
      ),
      body: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: BlocBuilder<SettingsUserCubit, SettingsUserState>(
          bloc: cubit,
          buildWhen: (prev, curr) => prev.user?.tokens != curr.user?.tokens,
          builder: (context, state) => CustomScrollView(
            slivers: [
              if (state.user!.tokens!
                  .where(
                    (e) => e.type == TokenTypeEnum.refresh,
                  )
                  .isNotEmpty)
                SliverText(
                  '${AppLocalizations.of(context)!.sessions}:',
                  style: Theme.of(context).textTheme.titleLarge,
                ),
              SliverList(
                delegate: SliverChildBuilderDelegate(
                  (context, i) {
                    final token = state.user!.tokens!
                        .where(
                          (e) => e.type == TokenTypeEnum.refresh,
                        )
                        .elementAt(i);

                    return TokenCard(
                      token: token,
                      onLogout: () {
                        cubit.logout(token);
                      },
                    );
                  },
                  childCount: state.user?.tokens
                          ?.where((e) => e.type == TokenTypeEnum.refresh)
                          .length ??
                      0,
                ),
              ),
              SliverPadding(
                padding: const EdgeInsets.only(top: 8),
                sliver: SliverToBoxAdapter(
                  child: TextWithIconButton(
                    title: '${AppLocalizations.of(context)!.llts}:',
                    icon: const Icon(Icons.add),
                    tooltip: AppLocalizations.of(context)!.lltCreate,
                    onPressed: () => _createLLTflow(context),
                  ),
                ),
              ),
              SliverList(
                delegate: SliverChildBuilderDelegate(
                  (context, i) {
                    final token = state.user!.tokens!
                        .where(
                          (e) => e.type == TokenTypeEnum.longlived,
                        )
                        .elementAt(i);

                    return TokenCard(
                      token: token,
                      onLogout: () {
                        cubit.deleteLongLivedToken(
                          token,
                        );
                      },
                    );
                  },
                  childCount: state.user?.tokens
                          ?.where(
                            (e) => e.type == TokenTypeEnum.longlived,
                          )
                          .length ??
                      0,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  void _createLLTflow(BuildContext context) async {
    final confirm = await askForConfirmation(
      context: context,
      title: Text(
        AppLocalizations.of(context)!.lltWarningTitle,
      ),
      content: Text(
        AppLocalizations.of(context)!.lltWarningContent,
      ),
      confirmText: AppLocalizations.of(context)!.okay,
    );
    if (!confirm) return;

    final request = await showDialog<LltRequest>(
      context: context,
      builder: (BuildContext context) => const LltCreateDialog(),
    );
    if (request == null) return;

    final token = await cubit.addLongLivedToken(
      request.name,
      scope: request.scope,
      householdId: request.householdId,
    );

    if (token == null || token.isEmpty) return;

    await askForConfirmation(
      context: context,
      showCancel: false,
      confirmText: AppLocalizations.of(context)!.done,
      title: Text(AppLocalizations.of(context)!.lltNotShownAgain),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SelectableText(token),
          const SizedBox(height: 8),
          Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              Builder(builder: (context) {
                return IconButton(
                  tooltip: AppLocalizations.of(context)!.share,
                  onPressed: () => SharePlus.instance.share(
                    ShareParams(text: token),
                  ),
                  icon: const Icon(Icons.ios_share_rounded),
                );
              }),
              Builder(builder: (context) {
                return IconButton(
                  tooltip: AppLocalizations.of(context)!.copied,
                  onPressed: () {
                    Clipboard.setData(ClipboardData(text: token));
                    Navigator.of(context).pop();
                    showSnackbar(
                      context: context,
                      content: Text(AppLocalizations.of(context)!.copied),
                    );
                  },
                  icon: const Icon(Icons.copy_rounded),
                );
              }),
            ],
          ),
        ],
      ),
    );
  }
}
