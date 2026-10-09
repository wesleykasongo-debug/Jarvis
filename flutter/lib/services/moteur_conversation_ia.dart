import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../models/user_quota.dart';
import 'supabase_service.dart';

/// Ton de réponse demandé au moteur (valeurs identiques à l'enum Python `Ton`).
enum Ton {
  naturel('Naturel'),
  amical('Amical'),
  professionnel('Professionnel'),
  concis('Concis'),
  pedagogue('Pédagogue'),
  enthousiaste('Enthousiaste');

  const Ton(this.libelle);
  final String libelle;
}

// --- Événements émis pendant une réponse ---------------------------------------

sealed class EvenementConversation {
  const EvenementConversation();
}

/// Le flux est ouvert : créer la carte de réponse (vide) avec [messageId].
final class DebutReponse extends EvenementConversation {
  const DebutReponse({required this.conversationId, required this.messageId});
  final String conversationId;
  final String messageId;
}

/// Un fragment est arrivé. [texteComplet] contient tout le texte reçu
/// jusque-là : il suffit de l'afficher tel quel dans la carte.
final class FragmentReponse extends EvenementConversation {
  const FragmentReponse({required this.fragment, required this.texteComplet});
  final String fragment;
  final String texteComplet;
}

/// Réponse terminée (et enregistrée côté serveur).
final class FinReponse extends EvenementConversation {
  const FinReponse({required this.messageId, required this.texteComplet, required this.tokens});
  final String messageId;
  final String texteComplet;
  final int tokens;
}

enum TypeErreur { quotaDepasse, nonAuthentifie, introuvable, reseau, serveur }

/// Échec. [texteRecu] contient l'éventuelle réponse partielle à conserver.
final class ErreurConversation extends EvenementConversation {
  const ErreurConversation(this.type, this.message, {this.texteRecu = ''});
  final TypeErreur type;
  final String message;
  final String texteRecu;
}

/// Client du backend FastAPI : envoi des messages et réception de la
/// réponse en streaming SSE.
///
/// ```dart
/// final moteur = MoteurConversationIA(baseUrl: Uri.parse('https://api.jarvis.app'));
/// await for (final ev in moteur.envoyer('Bonjour', ton: Ton.amical)) {
///   switch (ev) {
///     case DebutReponse(:final messageId):        // créer la carte
///     case FragmentReponse(:final texteComplet):  // mettre à jour la carte
///     case FinReponse():                          // figer la carte, lancer le TTS
///     case ErreurConversation(:final message):    // afficher l'erreur
///   }
/// }
/// ```
/// Annuler l'abonnement au flux (ex. bouton « stop ») coupe la connexion ;
/// le serveur enregistre alors la réponse partielle.
///
/// Flutter Web : le flux n'est progressif que si le client HTTP supporte le
/// streaming. Si les fragments arrivent d'un bloc, passer un client basé sur
/// fetch, par ex. `FetchClient(mode: RequestMode.cors)` du package `fetch_client`.
class MoteurConversationIA {
  MoteurConversationIA({
    required this.baseUrl,
    SupabaseService? auth,
    http.Client? client,
    this.delaiConnexion = const Duration(seconds: 30),
  })  : _auth = auth ?? SupabaseService.instance,
        _client = client ?? http.Client();

  final Uri baseUrl;
  final Duration delaiConnexion;
  final SupabaseService _auth;
  final http.Client _client;

  /// Dernier quota connu, mis à jour à chaque échange : brancher directement
  /// la `JaugeQuotaWidget` dessus.
  final ValueNotifier<UserQuota?> quota = ValueNotifier<UserQuota?>(null);

  /// Envoie [message] et émet la réponse au fil de l'eau.
  /// Sans [conversationId], le serveur crée une nouvelle conversation
  /// (son id arrive dans [DebutReponse]).
  Stream<EvenementConversation> envoyer(
    String message, {
    String? conversationId,
    Ton ton = Ton.naturel,
  }) async* {
    final String jeton;
    try {
      jeton = await _auth.jetonAcces();
    } on NonAuthentifieException catch (e) {
      yield ErreurConversation(TypeErreur.nonAuthentifie, e.message);
      return;
    }

    final requete = http.Request('POST', _endpoint('api/v1/chat/stream'))
      ..headers.addAll({
        'Authorization': 'Bearer $jeton',
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
      })
      ..body = jsonEncode({
        'message': message,
        if (conversationId != null) 'conversation_id': conversationId,
        'ton': ton.name,
      });

    final http.StreamedResponse reponse;
    try {
      reponse = await _client.send(requete).timeout(delaiConnexion);
    } on Exception {
      yield const ErreurConversation(TypeErreur.reseau, 'Impossible de joindre JARVIS. Vérifie ta connexion.');
      return;
    }

    if (reponse.statusCode != 200) {
      yield _erreurHttp(reponse.statusCode, await reponse.stream.bytesToString());
      return;
    }

    final texte = StringBuffer();
    String? evenement;
    final donnees = <String>[];
    var termine = false;

    try {
      final lignes = reponse.stream.transform(utf8.decoder).transform(const LineSplitter());
      await for (final ligne in lignes) {
        if (ligne.isEmpty) {
          // Ligne vide = fin d'un événement SSE
          if (evenement != null && donnees.isNotEmpty) {
            final ev = _decoder(evenement, donnees.join('\n'), texte);
            if (ev != null) {
              yield ev;
              if (ev is FinReponse || ev is ErreurConversation) termine = true;
            }
          }
          evenement = null;
          donnees.clear();
        } else if (ligne.startsWith(':')) {
          continue; // commentaire / keep-alive
        } else if (ligne.startsWith('event:')) {
          evenement = ligne.substring(6).trim();
        } else if (ligne.startsWith('data:')) {
          donnees.add(ligne.substring(5).trimLeft());
        }
      }
    } on Exception {
      yield ErreurConversation(
        TypeErreur.reseau,
        'Connexion interrompue pendant la réponse.',
        texteRecu: texte.toString(),
      );
      return;
    }

    if (!termine) {
      yield ErreurConversation(
        TypeErreur.reseau,
        'La réponse a été coupée avant la fin.',
        texteRecu: texte.toString(),
      );
    }
  }

  /// Quota temps réel depuis le backend (`GET /me/quota`).
  Future<UserQuota?> rafraichirQuota() async {
    try {
      final jeton = await _auth.jetonAcces();
      final reponse = await _client
          .get(_endpoint('me/quota'), headers: {'Authorization': 'Bearer $jeton'})
          .timeout(delaiConnexion);
      if (reponse.statusCode != 200) return quota.value;
      quota.value = UserQuota.fromJson(jsonDecode(reponse.body) as Map<String, dynamic>);
    } on Exception catch (e) {
      debugPrint('Quota indisponible : $e');
    }
    return quota.value;
  }

  void fermer() {
    _client.close();
    quota.dispose();
  }

  // --- Interne ---------------------------------------------------------------------

  Uri _endpoint(String chemin) {
    final base = baseUrl.path.endsWith('/') ? baseUrl.path : '${baseUrl.path}/';
    return baseUrl.replace(path: '$base$chemin');
  }

  EvenementConversation? _decoder(String evenement, String brut, StringBuffer texte) {
    final Map<String, dynamic> data;
    try {
      data = jsonDecode(brut) as Map<String, dynamic>;
    } on FormatException {
      return null;
    }

    switch (evenement) {
      case 'meta':
        _majQuota(data['quota']);
        return DebutReponse(
          conversationId: data['conversation_id'] as String,
          messageId: data['message_id'] as String,
        );
      case 'delta':
        final fragment = data['text'] as String? ?? '';
        texte.write(fragment);
        return FragmentReponse(fragment: fragment, texteComplet: texte.toString());
      case 'done':
        _majQuota(data['quota']);
        final usage = data['usage'] as Map<String, dynamic>? ?? const {};
        return FinReponse(
          messageId: data['message_id'] as String,
          texteComplet: texte.toString(),
          tokens: (usage['total_tokens'] as num?)?.toInt() ?? 0,
        );
      case 'error':
        return ErreurConversation(
          TypeErreur.serveur,
          data['message'] as String? ?? 'Erreur du moteur IA.',
          texteRecu: texte.toString(),
        );
      default:
        return null; // événement inconnu : ignoré (compatibilité future)
    }
  }

  void _majQuota(Object? json) {
    if (json is Map<String, dynamic>) quota.value = UserQuota.fromJson(json);
  }

  ErreurConversation _erreurHttp(int code, String corps) {
    Object? detail;
    try {
      detail = (jsonDecode(corps) as Map<String, dynamic>)['detail'];
    } on Object {
      detail = null;
    }

    if (code == 429 && detail is Map<String, dynamic>) {
      _majQuota(detail['quota']);
      return ErreurConversation(
        TypeErreur.quotaDepasse,
        detail['message'] as String? ?? 'Limite quotidienne atteinte.',
      );
    }

    final message = detail is String ? detail : null;
    return switch (code) {
      401 => ErreurConversation(TypeErreur.nonAuthentifie, message ?? 'Session expirée, reconnecte-toi.'),
      404 => ErreurConversation(TypeErreur.introuvable, message ?? 'Conversation introuvable.'),
      429 => const ErreurConversation(TypeErreur.quotaDepasse, 'Limite quotidienne atteinte.'),
      _ => ErreurConversation(TypeErreur.serveur, message ?? 'Erreur serveur ($code).'),
    };
  }
}
