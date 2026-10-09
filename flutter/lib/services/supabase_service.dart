import 'dart:async';
import 'dart:typed_data';

import 'package:supabase_flutter/supabase_flutter.dart';

import '../models/user_quota.dart';

/// Levée quand une action exige une session et qu'il n'y en a pas.
class NonAuthentifieException implements Exception {
  const NonAuthentifieException([this.message = 'Aucune session active']);
  final String message;

  @override
  String toString() => 'NonAuthentifieException: $message';
}

/// Authentification Supabase, jeton JWT pour FastAPI et accès aux données
/// de l'utilisateur (protégées par la RLS côté base).
///
/// À initialiser une fois dans `main()` :
/// ```dart
/// await SupabaseService.initialiser(url: '...', anonKey: '...');
/// ```
/// Seule la clé `anon` va dans l'app ; la clé `service_role` reste au backend.
class SupabaseService {
  SupabaseService._();

  static final SupabaseService instance = SupabaseService._();

  /// Marge avant expiration en deçà de laquelle on rafraîchit le jeton.
  static const _margeExpiration = Duration(seconds: 60);

  static Future<void> initialiser({required String url, required String anonKey}) {
    return Supabase.initialize(url: url, anonKey: anonKey);
  }

  SupabaseClient get _client => Supabase.instance.client;
  GoTrueClient get _auth => _client.auth;

  Future<String>? _rafraichissementEnCours;

  // --- Session -------------------------------------------------------------------

  User? get utilisateur => _auth.currentUser;
  bool get estConnecte => _auth.currentSession != null;
  Stream<AuthState> get changementsAuth => _auth.onAuthStateChange;

  Future<AuthResponse> connexion({required String email, required String motDePasse}) {
    return _auth.signInWithPassword(email: email, password: motDePasse);
  }

  /// `nomAffiche` est repris par le trigger SQL `handle_new_user`.
  Future<AuthResponse> inscription({
    required String email,
    required String motDePasse,
    String? nomAffiche,
  }) {
    return _auth.signUp(
      email: email,
      password: motDePasse,
      data: {if (nomAffiche != null) 'display_name': nomAffiche},
    );
  }

  Future<void> reinitialiserMotDePasse(String email) => _auth.resetPasswordForEmail(email);

  Future<void> deconnexion() => _auth.signOut();

  /// Jeton JWT valide à envoyer à FastAPI (`Authorization: Bearer ...`).
  ///
  /// Rafraîchi automatiquement s'il expire dans moins de 60 s. Les appels
  /// simultanés partagent un seul rafraîchissement.
  Future<String> jetonAcces() async {
    final session = _auth.currentSession;
    if (session == null) throw const NonAuthentifieException();

    final expiresAt = session.expiresAt;
    if (expiresAt == null) return session.accessToken;

    final expiration = DateTime.fromMillisecondsSinceEpoch(expiresAt * 1000, isUtc: true);
    if (expiration.difference(DateTime.now().toUtc()) > _margeExpiration) {
      return session.accessToken;
    }
    return _rafraichissementEnCours ??= _rafraichir().whenComplete(() {
      _rafraichissementEnCours = null;
    });
  }

  Future<String> _rafraichir() async {
    try {
      final reponse = await _auth.refreshSession();
      final session = reponse.session;
      if (session == null) throw const NonAuthentifieException('Session expirée');
      return session.accessToken;
    } on AuthException catch (e) {
      throw NonAuthentifieException(e.message);
    }
  }

  String get _uid {
    final id = utilisateur?.id;
    if (id == null) throw const NonAuthentifieException();
    return id;
  }

  // --- Profil ------------------------------------------------------------------

  Future<Map<String, dynamic>?> profil() {
    return _client.from('users').select().eq('id', _uid).maybeSingle();
  }

  /// Seules ces colonnes sont modifiables par le client (droits SQL).
  Future<void> mettreAJourProfil({
    String? nomAffiche,
    String? avatarUrl,
    Map<String, dynamic>? preferences,
  }) async {
    final champs = <String, dynamic>{
      if (nomAffiche != null) 'display_name': nomAffiche,
      if (avatarUrl != null) 'avatar_url': avatarUrl,
      if (preferences != null) 'preferences': preferences,
    };
    if (champs.isEmpty) return;
    await _client.from('users').update(champs).eq('id', _uid);
  }

  /// Quota lu directement dans Supabase. Préférer
  /// `MoteurConversationIA.rafraichirQuota()` (temps réel, via Redis).
  Future<UserQuota> quotaDepuisSupabase() async {
    final lignes = await _client.rpc('get_my_quota') as List<dynamic>;
    if (lignes.isEmpty) throw const NonAuthentifieException('Profil introuvable');
    return UserQuota.fromRpc(Map<String, dynamic>.from(lignes.first as Map));
  }

  // --- Conversations -------------------------------------------------------------

  Future<List<Map<String, dynamic>>> conversations({bool archivees = false, int limite = 50}) {
    return _client
        .from('conversations')
        .select('id, title, persona, archived, last_message_at')
        .eq('archived', archivees)
        .order('last_message_at', ascending: false)
        .limit(limite);
  }

  Future<List<Map<String, dynamic>>> messages(String conversationId) {
    return _client
        .from('messages')
        .select('id, role, content, attachments, created_at')
        .eq('conversation_id', conversationId)
        .order('created_at');
  }

  Future<void> renommerConversation(String conversationId, String titre) async {
    await _client.from('conversations').update({'title': titre}).eq('id', conversationId);
  }

  Future<void> archiverConversation(String conversationId, {bool archivee = true}) async {
    await _client.from('conversations').update({'archived': archivee}).eq('id', conversationId);
  }

  Future<void> supprimerConversation(String conversationId) async {
    await _client.from('conversations').delete().eq('id', conversationId);
  }

  // --- Documents (Storage) -------------------------------------------------------

  static const _bucket = 'documents';

  /// Envoie un fichier joint ; renvoie son chemin dans le bucket
  /// (`{user_id}/{conversation_id}/{nom}`), imposé par la RLS Storage.
  Future<String> televerserDocument({
    required String conversationId,
    required String nomFichier,
    required Uint8List contenu,
    required String typeMime,
  }) async {
    final chemin = '$_uid/$conversationId/$nomFichier';
    await _client.storage.from(_bucket).uploadBinary(
          chemin,
          contenu,
          fileOptions: FileOptions(contentType: typeMime, upsert: true),
        );
    return chemin;
  }

  /// URL temporaire pour afficher / télécharger un document privé.
  Future<String> urlDocument(String chemin, {Duration validite = const Duration(hours: 1)}) {
    return _client.storage.from(_bucket).createSignedUrl(chemin, validite.inSeconds);
  }

  Future<void> supprimerDocument(String chemin) async {
    await _client.storage.from(_bucket).remove([chemin]);
  }
}
